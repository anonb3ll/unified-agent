import json
from pathlib import Path

from unified_agent.tools.shell import run_cmd_check

FIXTURE_PATH = (
    Path(__file__).parents[1] / "packages" / "conformance" / "fixtures" / "shell-decisions.json"
)


def _normalize(result: dict) -> dict[str, object]:
    return {
        "ok": result["ok"],
        "verdict": result["verdict"],
        "mutates": result["mutates"],
        "finding_codes": sorted(finding["code"] for finding in result["findings"]),
    }


def test_python_reference_matches_shared_shell_fixture(isolated_env) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    for case in fixture["cases"]:
        actual = _normalize(run_cmd_check(case["command"], cwd=case["cwd"]))
        assert actual == case["expected"], case["id"]
