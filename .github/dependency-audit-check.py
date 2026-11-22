import datetime
import os
import subprocess
import sys
from pathlib import Path

import yaml

REQUIRED = ("id", "reason", "owner", "expiry")


def fail(message: str) -> None:
    print(f"dependency-audit ignore list: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_expiry(value: object, where: str) -> datetime.date:
    if isinstance(value, datetime.datetime):
        if value.time() != datetime.time.min or value.tzinfo is not None:
            fail(f"{where} expiry must be YYYY-MM-DD")
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.date.fromisoformat(text)
        except ValueError:
            fail(f"{where} expiry must be YYYY-MM-DD, got {value!r}")
        if parsed.isoformat() != text:
            fail(f"{where} expiry must be YYYY-MM-DD, got {value!r}")
        return parsed
    fail(f"{where} expiry must be YYYY-MM-DD, got {value!r}")
    raise AssertionError("unreachable")


def load_ids() -> list[str]:
    path = Path(os.environ.get("IGNORE_FILE", ".github/dependency-audit-ignores.yml"))
    if not path.is_file():
        fail(f"missing {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        fail(f"invalid YAML in {path}: {exc}")
    if not isinstance(data, dict) or "ignores" not in data:
        fail("must be a mapping with an 'ignores' key")
    ignores = data["ignores"]
    if not isinstance(ignores, list):
        fail("'ignores' must be a list (use [] when there are no exceptions)")
    today = datetime.datetime.now(datetime.UTC).date()
    ids: list[str] = []
    for index, entry in enumerate(ignores):
        where = f"ignores[{index}]"
        if not isinstance(entry, dict):
            fail(f"{where} must be a mapping")
        unknown = sorted(set(entry) - set(REQUIRED))
        if unknown:
            fail(f"{where} unexpected field(s): {', '.join(unknown)}")
        missing = [
            key
            for key in REQUIRED
            if key not in entry
            or entry[key] is None
            or (isinstance(entry[key], str) and entry[key].strip() == "")
        ]
        if missing:
            fail(f"{where} missing required field(s): {', '.join(missing)}")
        advisory_id = entry["id"]
        reason = entry["reason"]
        owner = entry["owner"]
        if (
            not isinstance(advisory_id, str)
            or not isinstance(reason, str)
            or not isinstance(owner, str)
        ):
            fail(f"{where} id, reason, and owner must be strings")
        expiry = parse_expiry(entry["expiry"], where)
        if expiry < today:
            fail(
                f"{where} id {advisory_id.strip()} expired on {expiry.isoformat()} "
                f"(today is {today.isoformat()} UTC)"
            )
        ids.append(advisory_id.strip())
    print(f"dependency-audit ignore list ok ({len(ids)} entries) from {path}")
    return ids


def audit_python(ids: list[str]) -> None:
    requirements = Path(os.environ["REQUIREMENTS"])
    lines = requirements.read_text().splitlines(True)
    kept: list[str] = []
    dropped = 0
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("-e ") or stripped.startswith("--editable "):
            dropped += 1
            continue
        kept.append(line)
    if dropped:
        print(f"omitted {dropped} editable project line(s) from the locked export before pip-audit")
    audited = requirements.with_name("requirements-audit.txt")
    audited.write_text("".join(kept))
    cmd = [
        "pip-audit",
        "-r",
        str(audited),
        "--disable-pip",
        "--progress-spinner",
        "off",
    ]
    for advisory_id in ids:
        cmd.extend(["--ignore-vuln", advisory_id])
    if ids:
        print("pip-audit ignore ids: " + ", ".join(ids))
    raise SystemExit(subprocess.call(cmd))


def main() -> None:
    ids = load_ids()
    mode = os.environ.get("DEPENDENCY_AUDIT_MODE", "validate")
    if mode == "validate":
        return
    if mode == "audit":
        audit_python(ids)
        return
    fail(f"unknown DEPENDENCY_AUDIT_MODE {mode!r}")


if __name__ == "__main__":
    main()
