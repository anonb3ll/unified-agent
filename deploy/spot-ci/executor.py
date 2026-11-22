"""Run one Cloud Build config on the Spot CI VM (task #2252).

    python3 executor.py <bucket> <run-id>

`scripts/cloud-ci.sh` with UA_CI_BACKEND=spot uploads a run to
gs://<bucket>/runs/<run-id>/ (the same source directory `gcloud builds
submit` would upload, as source.tgz, plus meta.json) and queues it. The VM's
startup script calls this once per queued run. It executes
deploy/cloudbuild/ci.yaml the way Cloud Build does, so one config defines CI
on both backends:

- every step is `docker run <name> <args>` with /workspace and the Docker
  socket mounted, so a step's own `docker run -v /workspace:...` resolves on
  the host, as on Cloud Build;
- user substitutions (`${_X}`, `$_X`) and the `$$` escape follow Cloud Build;
  an unescaped `$NAME` that is not a substitution is an error, as there;
- a step without `waitFor` waits for every earlier step; `waitFor: ['-']`
  starts at once;
- a failed step fails the build unless it sets `allowFailure`; a failed build
  cancels the steps still pending or running; `timeout` bounds the build.

Progress goes to gs://<bucket>/runs/<run-id>/state.json in the shape of
`gcloud builds describe --format=json` (status, createTime, startTime,
finishTime, substitutions, tags, steps[].id/status), which is what
cloud-ci.sh reads; the combined log goes to log.txt beside it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

WORKSPACE = Path("/workspace")
FINAL = {"SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED"}
_TOKEN = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def substitute(value: str, subs: dict[str, str]) -> str:
    """Cloud Build substitution: `$$` is a literal `$`; `${X}`/`$X` must be known."""

    def repl(m: re.Match[str]) -> str:
        if m.group(0) == "$$":
            return "$"
        name = m.group(1) or m.group(2)
        if name not in subs:
            raise ValueError(f"unknown substitution ${name}")
        return subs[name]

    return _TOKEN.sub(repl, value)


def seconds(value: str | None, default: float) -> float:
    if not value:
        return default
    m = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(value).strip())
    if not m:
        raise ValueError(f"bad duration {value!r}")
    return float(m.group(1))


def dependencies(steps: list[dict]) -> dict[str, list[str]]:
    """Each step's prerequisites, by Cloud Build's waitFor rules."""
    deps: dict[str, list[str]] = {}
    ids: list[str] = []
    for index, step in enumerate(steps):
        sid = step.get("id") or f"step-{index}"
        if sid in deps:
            raise ValueError(f"duplicate step id {sid}")
        wait = step.get("waitFor")
        if wait is None:
            deps[sid] = list(ids)
        elif wait == ["-"]:
            deps[sid] = []
        else:
            unknown = [w for w in wait if w not in ids]
            if unknown:
                raise ValueError(f"step {sid} waits for unknown or later steps {unknown}")
            deps[sid] = list(wait)
        ids.append(sid)
    return deps


def docker_command(step: dict, subs: dict[str, str], container: str) -> list[str]:
    cmd = [
        "docker", "run", "--rm", "--name", container,
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{WORKSPACE}:/workspace",
        "-w", "/workspace/" + substitute(step.get("dir", ""), subs).strip("/"),
    ]  # fmt: skip
    for env in step.get("env", []):
        cmd += ["-e", substitute(env, subs)]
    if step.get("entrypoint"):
        cmd += ["--entrypoint", substitute(step["entrypoint"], subs)]
    cmd.append(substitute(step["name"], subs))
    cmd += [substitute(arg, subs) for arg in step.get("args", [])]
    return cmd


class Build:
    """Schedule the steps of one config. `launch(sid, argv, log)` starts a step
    and returns an object with poll()/kill(); tests pass a fake."""

    def __init__(
        self,
        config: dict,
        subs: dict[str, str],
        launch: Callable,
        log: Callable[[str, str], None],
        *,
        deadline: float | None = None,
    ):
        self.steps = config["steps"]
        self.subs = {**config.get("substitutions", {}), **subs}
        self.deps = dependencies(self.steps)
        self.by_id = {sid: self.steps[i] for i, sid in enumerate(self.deps)}
        self.timeout = seconds(config.get("timeout"), 600.0)
        self.deadline = deadline
        self.launch = launch
        self.log = log
        self.status = {sid: "QUEUED" for sid in self.deps}
        self.build_status = "QUEUED"
        self.lock = threading.Lock()

    def snapshot(self) -> tuple[str, list[dict]]:
        with self.lock:
            return self.build_status, [{"id": s, "status": self.status[s]} for s in self.deps]

    def run(self, poll_interval: float = 1.0, clock: Callable[[], float] = time.monotonic) -> str:
        # Every argv is built (and every substitution checked) before anything runs.
        argv = {
            sid: docker_command(self.by_id[sid], self.subs, f"ua-ci-{i}")
            for i, sid in enumerate(self.deps)
        }
        deadline = clock() + self.timeout
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        step_deadline = {
            sid: seconds(self.by_id[sid].get("timeout"), float("inf")) for sid in self.deps
        }
        running: dict[str, tuple[object, float]] = {}
        failed = False
        with self.lock:
            self.build_status = "WORKING"
        while True:
            for sid, (proc, started) in list(running.items()):
                code = proc.poll()
                if code is None and clock() - started > step_deadline[sid]:
                    proc.kill()
                    code = proc.poll()
                    self._set(sid, "TIMEOUT")
                    failed = failed or not self.by_id[sid].get("allowFailure", False)
                    del running[sid]
                    continue
                if code is None:
                    continue
                del running[sid]
                ok = code == 0
                self._set(sid, "SUCCESS" if ok else "FAILURE")
                self.log(sid, f"exit {code}")
                if not ok and not self.by_id[sid].get("allowFailure", False):
                    failed = True
            if failed or clock() >= deadline:
                final = "FAILURE" if failed else "TIMEOUT"
                for sid, (proc, _) in running.items():
                    proc.kill()
                    self._set(sid, "CANCELLED" if failed else "TIMEOUT")
                for sid, st in self.status.items():
                    if st == "QUEUED":
                        self._set(sid, "CANCELLED")
                with self.lock:
                    self.build_status = final
                return final
            for sid in self.deps:
                if self.status[sid] != "QUEUED":
                    continue
                if all(self.status[d] in FINAL for d in self.deps[sid]):
                    self._set(sid, "WORKING")
                    self.log(sid, "start: " + " ".join(argv[sid]))
                    running[sid] = (self.launch(sid, argv[sid], self.log), clock())
            if not running and all(st in FINAL for st in self.status.values()):
                with self.lock:
                    self.build_status = "SUCCESS"
                return "SUCCESS"
            time.sleep(poll_interval)

    def _set(self, sid: str, status: str) -> None:
        with self.lock:
            self.status[sid] = status


class DockerStep:
    def __init__(self, sid: str, argv: list[str], log: Callable[[str, str], None]):
        self.container = argv[argv.index("--name") + 1]
        self.proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
        )
        self.reader = threading.Thread(target=self._pump, args=(sid, log), daemon=True)
        self.reader.start()

    def _pump(self, sid: str, log: Callable[[str, str], None]) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            log(sid, line.rstrip("\n"))

    def poll(self) -> int | None:
        code = self.proc.poll()
        if code is not None:
            self.reader.join(timeout=10)
        return code

    def kill(self) -> None:
        subprocess.run(["docker", "kill", self.container], capture_output=True)
        self.proc.kill()
        self.proc.wait()


def gcs_put(path: str, data: str) -> None:
    with tempfile.NamedTemporaryFile("w", delete=False) as fh:
        fh.write(data)
    subprocess.run(
        ["gcloud", "storage", "cp", "-q", fh.name, path], check=True, capture_output=True
    )
    Path(fh.name).unlink()


def main(bucket: str, run_id: str) -> int:
    # Count setup and source staging toward the runner's budget as well.
    limit = os.environ.get("UA_SPOT_BUILD_TIMEOUT_S")
    deadline = time.monotonic() + int(limit) if limit is not None else None
    prefix = f"gs://{bucket}/runs/{run_id}"
    meta = json.loads(
        subprocess.run(
            ["gcloud", "storage", "cat", f"{prefix}/meta.json"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    log_lines: list[str] = []
    log_lock = threading.Lock()

    def log(sid: str, line: str) -> None:
        with log_lock:
            log_lines.append(f"[{sid}] {line}\n")

    state = {
        "id": run_id,
        "status": "WORKING",
        "createTime": meta["createTime"],
        "startTime": now(),
        "substitutions": meta["substitutions"],
        "tags": meta["tags"],
        "steps": [],
    }

    def publish(final: bool = False) -> None:
        state["status"], state["steps"] = build.snapshot() if build else (state["status"], [])
        if final:
            state["finishTime"] = now()
        with log_lock:
            text = "".join(log_lines)
        gcs_put(f"{prefix}/log.txt", text)
        gcs_put(f"{prefix}/state.json", json.dumps(state))

    build: Build | None = None
    subprocess.run(["rm", "-rf", str(WORKSPACE)], check=True)
    WORKSPACE.mkdir()
    try:
        subprocess.run(
            ["gcloud", "storage", "cp", "-q", f"{prefix}/source.tgz", "/tmp/source.tgz"], check=True
        )
        subprocess.run(["tar", "-xzf", "/tmp/source.tgz", "-C", str(WORKSPACE)], check=True)
        import yaml  # python3-yaml on the VM; imported here so the tests need only the module

        config = yaml.safe_load((WORKSPACE / meta["config"]).read_text())
        build = Build(
            config,
            {**meta["substitutions"], "BUILD_ID": run_id},
            DockerStep,
            log,
            deadline=deadline,
        )
    except Exception as exc:  # noqa: BLE001 - any setup failure is the run's INTERNAL_ERROR
        log("runner", f"setup failed: {exc!r}")
        state["status"] = "INTERNAL_ERROR"
        publish(final=True)
        return 1

    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(30):
            try:
                publish()
            except subprocess.CalledProcessError:
                pass

    beat = threading.Thread(target=heartbeat, daemon=True)
    publish()
    beat.start()
    try:
        build.run()
    except Exception as exc:  # noqa: BLE001
        log("runner", f"runner failed: {exc!r}")
        with build.lock:
            build.build_status = "INTERNAL_ERROR"
    stop.set()
    beat.join()
    publish(final=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
