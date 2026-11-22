"""Spot VM backend for scripts/cloud-ci.sh (task #2252).

    spot_ci.py submit <src-dir> <sha>   prints the new run id
    spot_ci.py describe <run-id>        prints the run as `gcloud builds describe --format=json`
    spot_ci.py describe <run-id> status|createTime   prints that one field

Cloud Build bills about $0.016 per minute on E2_HIGHCPU_8; the same machine as
a Spot VM costs a few cents an hour. `submit` uploads the directory
cloud-ci.sh would have passed to `gcloud builds submit` (it holds ci.yaml and
ci-step.sh) to gs://<bucket>/runs/<id>/, queues it, refreshes the VM's startup
script and executor from the commit under test, and starts the VM. The VM runs the
queue (deploy/spot-ci/startup.sh, executor.py) and powers itself off.

`describe` reads the run's state.json. While the run is still QUEUED or
WORKING it also makes sure the VM is up: a VM that stopped with work left
(Spot preemption, the duration cap, or a run queued as it shut down) is
started again; the VM retries an interrupted run once.

Env: UA_CI_GCP_PROJECT (required), UA_CI_SPOT_ZONE (us-central1-a),
UA_CI_SPOT_INSTANCE (ua-ci-runner), UA_CI_SPOT_BUCKET
(<project>-spot-ci). deploy/spot-ci/provision.sh creates them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_ID = re.compile(r"spot-[0-9a-f]{32}")
SETTLED = {"SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"}


def settings() -> dict[str, str]:
    project = os.environ.get("UA_CI_GCP_PROJECT", "")
    if not project:
        sys.exit("set UA_CI_GCP_PROJECT")
    return {
        "project": project,
        "zone": os.environ.get("UA_CI_SPOT_ZONE", "us-central1-a"),
        "instance": os.environ.get("UA_CI_SPOT_INSTANCE", "ua-ci-runner"),
        "bucket": os.environ.get("UA_CI_SPOT_BUCKET", f"{project}-spot-ci"),
    }


def gcloud(*args: str, stdin: str | None = None) -> str:
    out = subprocess.run(
        ["gcloud", *args, "--quiet"], input=stdin, capture_output=True, text=True, check=False
    )
    if out.returncode:
        raise RuntimeError(f"gcloud {' '.join(args[:3])}: {out.stderr.strip()[-400:]}")
    return out.stdout


def config_tags(config: str, subs: dict[str, str]) -> list[str]:
    """The top-level `tags:` block list, substituted. cloud-ci.sh has already
    refused a config whose tags are not a plain block list."""
    lines = config.split("\n")
    tags: list[str] = []
    for i, line in enumerate(lines):
        if line.rstrip() == "tags:":
            for item in lines[i + 1 :]:
                if not item.startswith("- "):
                    break
                tag = item[2:].strip()
                for key, value in subs.items():
                    tag = tag.replace("${" + key + "}", value)
                tags.append(tag)
    return tags


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def start_vm(cfg: dict[str, str]) -> None:
    status = gcloud(
        "compute", "instances", "describe", cfg["instance"], f"--project={cfg['project']}",
        f"--zone={cfg['zone']}", "--format=value(status)",
    ).strip()  # fmt: skip
    if status in {"TERMINATED", "STOPPED", "SUSPENDED"}:
        try:
            gcloud(
                "compute", "instances", "start", cfg["instance"], f"--project={cfg['project']}",
                f"--zone={cfg['zone']}", "--async",
            )  # fmt: skip
        except RuntimeError as exc:
            # A concurrent start, or a Spot capacity stockout: the next
            # describe tries again. Neither is the run's result.
            print(f"spot-ci: start {cfg['instance']}: {exc}", file=sys.stderr)


def commit_file(sha: str, name: str) -> bytes:
    """`deploy/spot-ci/<name>` as committed at `sha`. The runner scripts that go
    to the VM come from the commit under test, never from whatever checkout the
    submitter happens to run in (a stale worktree would downgrade the VM)."""
    out = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{sha}:deploy/spot-ci/{name}"],
        capture_output=True, check=False,
    )  # fmt: skip
    if out.returncode:
        known = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True, check=False,
        ).returncode == 0  # fmt: skip
        local = ROOT / "deploy" / "spot-ci" / name
        if known and local.is_file():
            # A commit from before the runner existed carries no copy of its
            # own; the runner it needs is the one this checkout ships.
            print(
                f"spot-ci: deploy/spot-ci/{name} is not in {sha[:12]}; using this checkout's copy",
                file=sys.stderr,
            )
            return local.read_bytes()
        raise RuntimeError(
            f"deploy/spot-ci/{name} is not readable at {sha}: {out.stderr.decode().strip()[-200:]}"
        )
    return out.stdout


def submit(src: Path, sha: str) -> str:
    cfg = settings()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        sys.exit(f"not a full commit SHA: {sha}")
    run_id = f"spot-{uuid.uuid4().hex}"
    prefix = f"gs://{cfg['bucket']}/runs/{run_id}"
    subs = {"_SOURCE_SHA": sha}
    created = now()
    meta = {
        "createTime": created,
        "config": "ci.yaml",
        "substitutions": subs,
        "tags": config_tags((src / "ci.yaml").read_text(), subs),
    }
    state = {**meta, "id": run_id, "status": "QUEUED", "steps": []}
    del state["config"]
    startup, executor = commit_file(sha, "startup.sh"), commit_file(sha, "executor.py")
    with tempfile.TemporaryDirectory() as tmp:
        # Refresh the VM's startup script before anything is queued: once the
        # queue marker exists the run will execute, so every later step must
        # still return its id. A running VM keeps the script it booted with;
        # each run brings its own executor.py.
        (Path(tmp) / "startup.sh").write_bytes(startup)
        (Path(tmp) / "executor.py").write_bytes(executor)
        gcloud(
            "compute", "instances", "add-metadata", cfg["instance"], f"--project={cfg['project']}",
            f"--zone={cfg['zone']}",
            f"--metadata-from-file=startup-script={Path(tmp) / 'startup.sh'}",
            f"--metadata=ua-ci-bucket={cfg['bucket']}",
        )  # fmt: skip
        bundle = Path(tmp) / "source.tgz"
        with tarfile.open(bundle, "w:gz") as tar:
            for entry in sorted(src.iterdir()):
                tar.add(entry, arcname=entry.name)
        files = {"meta.json": json.dumps(meta), "state.json": json.dumps(state)}
        for name, text in files.items():
            (Path(tmp) / name).write_text(text)
        gcloud(
            "storage", "cp", str(bundle), *(str(Path(tmp) / n) for n in files),
            str(Path(tmp) / "executor.py"), f"{prefix}/",
        )  # fmt: skip
        # Queue order is submission order: the marker sorts by creation time.
        marker = Path(tmp) / "marker"
        marker.write_text(run_id)
        stamp = created.replace(":", "").replace("-", "").replace(".", "")
        gcloud("storage", "cp", str(marker), f"gs://{cfg['bucket']}/queue/{stamp}_{run_id}")
    start_vm(cfg)
    return run_id


def describe(run_id: str) -> dict:
    cfg = settings()
    if not RUN_ID.fullmatch(run_id):
        sys.exit(f"not a spot CI run id: {run_id}")
    path = f"gs://{cfg['bucket']}/runs/{run_id}/state.json"
    try:
        state = json.loads(gcloud("storage", "cat", path))
    except RuntimeError:
        # Seen once per run while the VM overwrites state.json: read again.
        time.sleep(3)
        state = json.loads(gcloud("storage", "cat", path))
    if state.get("status") not in SETTLED:
        start_vm(cfg)
    return state


def main(argv: list[str]) -> int:
    match argv:
        case ["submit", src, sha]:
            print(submit(Path(src), sha))
        case ["describe", run_id]:
            print(json.dumps(describe(run_id)))
        case ["describe", run_id, ("status" | "createTime") as field]:
            print(describe(run_id).get(field, ""))
        case _:
            print(__doc__.strip().split("\n\n")[0], file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except RuntimeError as exc:
        sys.exit(f"spot-ci: {exc}")
