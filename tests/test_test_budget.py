"""scripts/test_budget.py keeps the test suite from growing back (task #2233).

The tree-state test is the hard guard: it runs in the full suite, so a new
issue-numbered test file or an oversized one fails CI wherever tests run. The
rest pin the per-change count the `gates` step runs on each pull request,
including one regression test per finding of the first review (PR #1323).
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "test_budget.py"
spec = importlib.util.spec_from_file_location("test_budget", SCRIPT)
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


def test_tree_is_within_budget():
    problems = budget.check_tree()
    assert not problems, "\n".join(problems)


def _tree(tmp_path, files):
    for rel, lines in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n" * lines)
    return tmp_path


def test_new_issue_numbered_file_fails_in_any_directory(tmp_path):
    root = _tree(
        tmp_path,
        {
            "tests/test_old_1234.py": 1,
            "tests/sub/test_thing_2233.py": 1,
            "tests/test_sha256.py": 1,
            "tests/test_utf_8.py": 1,
        },
    )
    problems = budget.check_tree(root, frozenset({"tests/test_old_1234.py"}), {})
    assert len(problems) == 1
    assert problems[0].startswith("tests/sub/test_thing_2233.py: test file named after an issue")
    assert "module's existing test file" in problems[0]


def test_size_cap_and_grandfathered_ceiling(tmp_path):
    cap = budget.LINE_CAP
    root = _tree(
        tmp_path,
        {
            "tests/test_new.py": cap + 1,
            "tests/test_at_cap.py": cap,
            "tests/test_big.py": 1900,
            "tests/test_runaway.py": 2501,
            "tests/widget_test.py": cap + 1,
        },
    )
    sizes = {"tests/test_big.py": 2000, "tests/test_runaway.py": 2500}
    problems = budget.check_tree(root, frozenset(), sizes)
    assert len(problems) == 3, problems
    assert problems[0].startswith(f"tests/test_new.py: {cap + 1} lines, above the {cap}-line cap")
    assert problems[1].startswith(
        "tests/test_runaway.py: 2501 lines, above its grandfathered ceiling of 2500"
    )
    # Suffix-named modules are test files too (review: they once escaped the cap).
    assert problems[2].startswith(f"tests/widget_test.py: {cap + 1} lines")


def test_allowlists_may_only_shrink(tmp_path):
    root = _tree(tmp_path, {"tests/test_shrunk.py": 1600, "tests/test_small.py": 1200})
    problems = budget.check_tree(
        root,
        frozenset({"tests/test_gone_1234.py"}),
        {
            "tests/test_gone.py": 2000,
            "tests/test_small.py": 2000,
            "tests/test_shrunk.py": 2500,
        },
    )
    assert len(problems) == 4, problems
    joined = "\n".join(problems)
    assert "test_gone_1234.py no longer exists: remove it from GRANDFATHERED_ISSUE_FILES" in joined
    assert "test_gone.py no longer exists: remove it from GRANDFATHERED_SIZES" in joined
    assert "test_small.py is 1200 lines, within the 1500-line cap: remove it" in joined
    assert "test_shrunk.py shrank to 1600 lines: lower its GRANDFATHERED_SIZES ceiling" in joined
    assert "from 2500 to 2000" in joined


def test_embedded_ceilings_are_rounded_up_past_the_cap():
    assert budget.ceiling_for(1501) == 2000
    assert budget.ceiling_for(2000) == 2500
    for rel, ceiling in budget.GRANDFATHERED_SIZES.items():
        assert ceiling % budget.CEILING_STEP == 0 and ceiling > budget.LINE_CAP, rel


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "--initial-branch=main", "--template=")
    _git(repo, "config", "user.name", "CI fixture")
    _git(repo, "config", "user.email", "ci@example.invalid")
    return repo


def _commit(repo, message, files=None, delete=()):
    for rel, text in (files or {}).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for rel in delete:
        (repo / rel).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _tests(n, prefix="case"):
    return "".join(f"def test_{prefix}_{i}():\n    pass\n\n\n" for i in range(n))


def _count(repo, base, head="HEAD", *extra):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "count", "--repo", str(repo), "--base", base]
        + ["--head", head, *extra],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _merge_parent(repo, *extra):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "merge-parent", "--repo", str(repo), *extra],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_count_starts_at_the_merge_commits_base_not_a_stale_base_sha(tmp_path):
    """Task #2430: the gate counted the base branch's own commits into a PR.

    GitHub's pull-request merge commit merges the PR head into the CURRENT base
    tip. Counting from `pull_request.base.sha` — the base tip at the PR's last
    synchronize — credits the PR with every commit the base branch gained since:
    this fixture's PR adds 3 tests, the base gained 5, and the stale count
    reported +8 over 2 files.
    """
    repo = _repo(tmp_path)
    stale_base = _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    pr_head = _commit(repo, "pr adds three", files={"tests/test_pr.py": _tests(3, "pr")})
    _git(repo, "checkout", "-q", "main")
    base_tip = _commit(repo, "base gains five", files={"tests/test_main.py": _tests(5, "main")})
    _git(repo, "merge", "-q", "--no-ff", "pr", "-m", "Merge pull request #1")

    assert budget.merge_parent(repo, "HEAD", pr_head) == base_tip
    stale = _count(repo, stale_base)
    assert stale.returncode == 0, stale.stderr
    assert "net +8 test functions" in stale.stdout
    fixed = _count(repo, base_tip)
    assert fixed.returncode == 0, fixed.stderr
    assert "net +3 test functions" in fixed.stdout

    printed = _merge_parent(repo, "--pr-head", pr_head)
    assert printed.returncode == 0, printed.stderr
    assert printed.stdout.strip() == base_tip


def test_merge_parent_refuses_a_commit_that_is_not_a_merge(tmp_path):
    """Counting a single-parent commit against its parent would pass any change."""
    repo = _repo(tmp_path)
    _commit(repo, "first")
    _commit(repo, "second")

    refused = _merge_parent(repo)
    assert refused.returncode == 2
    assert "not a pull-request merge commit" in refused.stderr


def test_merge_parent_refuses_a_merge_of_a_different_head(tmp_path):
    """A two-parent commit that is not this PR's merge would measure another change."""
    repo = _repo(tmp_path)
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "other")
    other = _commit(repo, "other work", files={"tests/test_other.py": _tests(2, "other")})
    _git(repo, "checkout", "-q", "main")
    base_tip = _commit(repo, "advance", files={"tests/test_main.py": _tests(2, "main")})
    _git(repo, "merge", "-q", "--no-ff", "other", "-m", "Merge other into main")

    # The first parent alone is still the base, when no head is claimed.
    assert budget.merge_parent(repo, "HEAD") == base_tip
    assert budget.merge_parent(repo, "HEAD", other) == base_tip
    refused = _merge_parent(repo, "--pr-head", "0" * 40)
    assert refused.returncode == 2
    assert "not the merge of pull request head" in refused.stderr


def test_count_reads_collected_tests_from_the_syntax_tree():
    source = b'''
import pytest

@pytest.mark.parametrize("x", [1, 2])
def test_module(x): pass

async def test_async(): pass

def helper_test(): pass

if True:
    def test_under_if(): pass

class TestGroup:
    @pytest.mark.parametrize("y", [1])
    def test_method(self, y): pass

    def helper(self): pass

    class TestNested:
        def test_inner(self): pass

class Helpers:
    def test_counted_conservatively(self): pass

EXAMPLE = """
def test_inside_a_string(): pass
"""
'''
    assert budget.count_source(source) == (6, 2, [])


def _defs(n, indent="", prefix="case", args=""):
    return "".join(f"{indent}def test_{prefix}_{i}({args}):\n{indent}    pass\n" for i in range(n))


def test_unittest_case_methods_count_whatever_the_class_is_called(tmp_path):
    """Review round 2: a TestCase subclass not named Test* once counted 0."""
    repo = _repo(tmp_path)
    base = _commit(repo, "base")
    cases = "import unittest\n\n\nclass BudgetCases(unittest.TestCase):\n"
    _commit(repo, "cases", {"tests/test_cases.py": cases + _defs(61, "    ", args="self")})
    result = _count(repo, base)
    assert result.returncode == 1 and "net +61 test functions" in result.stdout


def test_defs_under_loops_and_match_arms_count():
    """Review round 2: for/while/match bodies were once skipped."""
    loop = "for once in [1]:\n" + _defs(61, "    ")
    assert budget.count_source(loop.encode()) == (61, 0, [])
    arm = "match 1:\n    case 1:\n" + _defs(61, "        ") + "    case _:\n        pass\n"
    assert budget.count_source(arm.encode()) == (61, 0, [])
    spin = "while True:\n" + _defs(2, "    ") + "    break\n"
    assert budget.count_source(spin.encode()) == (2, 0, [])


def test_helpers_and_conftest_defs_count_wherever_they_live(tmp_path):
    """Review round 3: helpers re-exported into a test module once escaped the count."""
    repo = _repo(tmp_path)
    base = _commit(repo, "base", {"tests/test_home.py": _defs(1)})
    _commit(
        repo,
        "helpers",
        {
            "tests/helpers.py": _defs(60, prefix="shared"),
            "tests/conftest.py": _defs(1, prefix="hooked"),
            "tests/test_probe.py": "from tests.helpers import *  # noqa: F403\n",
        },
    )
    result = _count(repo, base)
    assert result.returncode == 1 and "net +61 test functions" in result.stdout


# The guarded names are spelled at runtime. This file is itself under tests/,
# and spelling them in a literal would make every change to it an unknown count.
NAMES = {
    "@T": "_" * 2 + "test" + "_" * 2,
    "@MAKEITEM": "pytest_pyco" + "llect_makeitem",
    "@COLLECT": "pytest_co" + "llect_file",
}


def _spell(text):
    for token, name in NAMES.items():
        text = text.replace(token, name)
    return text


UNCOUNTABLE_SOURCES = [
    # Review round 3: reassigned and annotated, nested, tuple-unpacked, via setattr.
    "@T = False\n" + _defs(1) + "@T: bool = True\n",
    "if True:\n    @T = False\n" + _defs(1),
    "@T, x = False, 0\n",
    "class Helper:\n    pass\n\n\nsetattr(Helper, '@T', False)\n",
    "def @MAKEITEM(collector, name, obj):\n    pass\n",
    "def @COLLECT(file_path, parent):\n    pass\n",
    # Review round 4: match captures, star and mapping rest, class keywords.
    "match 1:\n    case @T:\n        pass\n",
    "match []:\n    case [*@T]:\n        pass\n",
    "match {}:\n    case {**@T}:\n        pass\n",
    "match 1:\n    case int(@T=1):\n        pass\n",
    "def f():\n    global @T\n",
]


def test_uncountable_constructs_are_found_in_any_node():
    """Review rounds 3 and 4: detection is generic over AST fields, not a node list."""
    for template in UNCOUNTABLE_SOURCES:
        _, _, found = budget.count_source(_spell(template).encode(), "tests/test_a.py")
        assert found, template


def test_folded_and_escaped_literals_are_detected():
    """Review round 4: the parser folds implicit concatenation and escapes; so does detection."""
    for source in (
        'x = "__" "test__"\n',
        'x = "\\x5f_test__"\n',
        'x = b"\\x5f\\x5ftest__"\n',
        'x = ("pytest_pyco"\n     "llect_makeitem")\n',
    ):
        _, _, found = budget.count_source(source.encode(), "tests/test_a.py")
        assert found, source
    # Built at runtime is deliberate evasion and out of scope (see the threat model).
    assert budget.count_source(b'x = "__" + "test__"\n')[2] == []


def test_uncountable_head_files_make_the_count_unknown(tmp_path):
    for i, (path, source) in enumerate(
        (
            ("tests/test_a.py", UNCOUNTABLE_SOURCES[0]),
            ("tests/sub/conftest.py", UNCOUNTABLE_SOURCES[5]),
        )
    ):
        repo = _repo(tmp_path / str(i))
        base = _commit(repo, "base", {"tests/test_home.py": _defs(1)})
        _commit(repo, "construct", {path: _spell(source)})
        result = _count(repo, base)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "COUNT UNKNOWN" in result.stdout and f"HEAD:{path}" in result.stdout

    repo = _repo(tmp_path / "link")
    base = _commit(repo, "cases", {"tests/cases.py": _defs(61)})
    (repo / "tests/test_link.py").symlink_to("cases.py")
    _commit(repo, "link")
    linked = _count(repo, base)
    assert linked.returncode == 1
    assert "COUNT UNKNOWN" in linked.stdout and "HEAD:tests/test_link.py: symlink" in linked.stdout


def test_unchanged_construct_on_the_base_does_not_affect_other_changes(tmp_path):
    """Review round 4: a construct that landed once must not fail every later PR."""
    repo = _repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests/test_link.py").symlink_to("test_home.py")
    base = _commit(
        repo,
        "constructs landed with a trailer",
        {"tests/test_home.py": _defs(1), "tests/conftest.py": _spell(UNCOUNTABLE_SOURCES[4])},
    )
    _commit(repo, "unrelated", {"tests/test_other.py": _defs(2)})
    result = _count(repo, base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("test budget: net +2 test functions")
    assert "COUNT UNKNOWN" not in result.stdout


def test_removing_a_construct_makes_the_count_unknown(tmp_path):
    """Either side of a changed path counts: removing a construct needs a trailer."""
    repo = _repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests/test_link.py").symlink_to("test_home.py")
    helpers = "class Helpers:\n    @T = False\n" + _defs(3, "    ", "helper", "self")
    base = _commit(
        repo, "base", {"tests/test_home.py": _defs(1), "tests/test_helpers.py": _spell(helpers)}
    )
    _commit(
        repo,
        "remove constructs",
        {"tests/test_helpers.py": _defs(4, prefix="real")},
        delete=["tests/test_link.py"],
    )
    result = _count(repo, base)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "COUNT UNKNOWN" in result.stdout
    assert f"{base}:tests/test_link.py: symlink" in result.stdout
    assert f"{base}:tests/test_helpers.py" in result.stdout

    messages = tmp_path / "messages"
    messages.write_bytes(b"test: drop helpers\n\nTest-Budget: removal checked by hand\n")
    allowed = _count(repo, base, "HEAD", "--messages", str(messages))
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert "Allowed by Test-Budget: removal checked by hand" in allowed.stdout


def test_deleting_only_the_dunder_line_is_unknown(tmp_path):
    """Base and head both hold 61 defs (net 0), but pytest goes from 0 to 61 collected."""
    repo = _repo(tmp_path)
    cases = _defs(61, "    ", args="self")
    base = _commit(
        repo,
        "hidden cases",
        {"tests/test_cases.py": _spell("class TestCases:\n    @T = False\n" + cases)},
    )
    _commit(repo, "enable", {"tests/test_cases.py": "class TestCases:\n" + cases})
    result = _count(repo, base)
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.startswith("test budget: net +0 test functions")
    assert "COUNT UNKNOWN" in result.stdout and f"{base}:tests/test_cases.py" in result.stdout


def test_trailer_overrides_an_unknown_count(tmp_path):
    repo = _repo(tmp_path)
    base = _commit(repo, "base", {"tests/test_home.py": _defs(1)})
    _commit(repo, "hook", {"tests/conftest.py": _spell(UNCOUNTABLE_SOURCES[4])})
    messages = tmp_path / "messages"
    messages.write_bytes(b"test: add a collector\n\nTest-Budget: checked by hand, +0 tests\n")
    result = _count(repo, base, "HEAD", "--messages", str(messages))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "COUNT UNKNOWN" in result.stdout
    assert "Allowed by Test-Budget: checked by hand, +0 tests" in result.stdout


def test_renames_moves_and_folds_count_zero(tmp_path):
    repo = _repo(tmp_path)
    base = _commit(
        repo,
        "base",
        {
            "tests/test_widget_1234.py": _tests(2, "widget"),
            "tests/test_home.py": _tests(1, "home"),
            "tests/test_folded_777.py": _tests(3, "folded"),
        },
    )
    _git(repo, "mv", "tests/test_widget_1234.py", "tests/test_widget.py")
    _commit(
        repo,
        "fold",
        {"tests/test_home.py": _tests(1, "home") + _tests(3, "folded")},
        delete=["tests/test_folded_777.py"],
    )
    result = _count(repo, base)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("test budget: net +0 test functions, net +0 parametrize")


def test_string_fixture_swapped_for_real_tests_counts_them(tmp_path):
    """Review finding: `def test_` text inside a string once hid real additions."""
    repo = _repo(tmp_path)
    fixture = 'SAMPLE = """\n' + _tests(61, "example") + '"""\n'
    base = _commit(repo, "fixture", {"tests/test_parser.py": fixture})
    _commit(repo, "real tests", {"tests/test_parser.py": _tests(61, "example")})
    result = _count(repo, base)
    assert result.returncode == 1
    assert result.stdout.startswith("test budget: net +61 test functions")
    assert "OVER BUDGET" in result.stdout and "Test-Budget: <reason>" in result.stdout
    assert _count(repo, base, "HEAD", "--budget", "61").returncode == 0


def test_non_ascii_test_path_is_counted(tmp_path):
    """Review finding: a Git-quoted path was once dropped from the count."""
    repo = _repo(tmp_path)
    base = _commit(repo, "base", {"tests/test_home.py": _tests(1)})
    _commit(repo, "unicode", {"tests/test_é.py": _tests(61), "tests/données/test_ü.py": _tests(1)})
    result = _count(repo, base)
    assert result.returncode == 1
    assert result.stdout.startswith("test budget: net +62 test functions")


def test_unreadable_input_fails_instead_of_passing(tmp_path):
    """Review finding: incomplete input once read as `net +0: OK`."""
    repo = _repo(tmp_path)
    base = _commit(repo, "base", {"tests/test_home.py": _tests(1)})
    missing = _count(repo, "0" * 40)
    assert missing.returncode == 2 and "ERROR" in missing.stderr
    assert missing.stdout == ""

    _commit(repo, "broken", {"tests/test_broken.py": "def test_x(:\n"})
    broken = _count(repo, base)
    assert broken.returncode == 2 and "test_broken.py: cannot parse" in broken.stderr

    absent = _count(repo, "HEAD", "HEAD", "--messages", str(tmp_path / "absent"))
    assert absent.returncode == 2 and "cannot read commit messages" in absent.stderr
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n")
    malformed = _count(repo, "HEAD", "HEAD", "--messages", str(bad), "--messages-format", "jsonl")
    assert malformed.returncode == 2


TRAILER_CASES = [
    ("test: add cases\n\nBody.\n\nTest-Budget: new wire protocol\nCo-Authored-By: A <a@b>\n", True),
    ("test: add cases\n\ntest-budget: lower-case key\n", True),
    (
        "docs: explain\n\nExample:\n```\nTest-Budget: example only\n```\nand some prose after.\n",
        False,
    ),
    ("test: add cases\n\nprose first\nTest-Budget: inside prose\n", False),
    ("test: add cases\n\nTest-Budget:\nCo-Authored-By: A <a@b>\n", False),
    ("Test-Budget: subject line only\n", False),
]


def test_override_needs_a_trailer_that_closes_one_message():
    """Review finding: a quoted example line once authorized 61 additions."""
    for message, allowed in TRAILER_CASES:
        assert bool(budget.override_reason([message])) is allowed, message
        fallback = any(
            k.lower() == "test-budget" and v for k, v in budget._fallback_trailers(message)
        )
        assert fallback is allowed, message
    # Messages are separate records: a trailer is never assembled across two.
    assert budget.override_reason(["fix: one\n\nTest-Budget:", " reason\n"]) is None


def test_trailer_overrides_in_both_message_formats(tmp_path):
    repo = _repo(tmp_path)
    base = _commit(repo, "base")
    _commit(
        repo,
        "test: add cases\n\nTest-Budget: contract matrix for a new protocol",
        {"tests/test_new.py": _tests(61)},
    )
    nul = tmp_path / "messages"
    nul.write_bytes(_git(repo, "log", "-z", "--format=%B", f"{base}..HEAD").encode() + b"\0")
    allowed = _count(repo, base, "HEAD", "--messages", str(nul))
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert "allowed by Test-Budget: contract matrix for a new protocol" in allowed.stdout

    jsonl = tmp_path / "messages.jsonl"
    jsonl.write_text(json.dumps(TRAILER_CASES[2][0]) + "\n")
    quoted = _count(repo, base, "HEAD", "--messages", str(jsonl), "--messages-format", "jsonl")
    assert quoted.returncode == 1


def test_stacked_child_does_not_inherit_its_parents_trailer(tmp_path):
    """Review finding: a stacked child once passed on its parent PR's trailer."""
    repo = _repo(tmp_path)
    main = _commit(repo, "base")
    parent = _commit(
        repo, "test: parent\n\nTest-Budget: parent reason", {"tests/test_a.py": _tests(61, "a")}
    )
    _commit(repo, "test: child", {"tests/test_b.py": _tests(61, "b")})
    messages = tmp_path / "child-messages"
    messages.write_bytes(_git(repo, "log", "-z", "--format=%B", f"{parent}..HEAD").encode())
    child = _count(repo, parent, "HEAD", "--messages", str(messages))
    assert child.returncode == 1 and "net +61" in child.stdout
    messages.write_bytes(_git(repo, "log", "-z", "--format=%B", f"{main}..{parent}").encode())
    assert _count(repo, main, parent, "--messages", str(messages)).returncode == 0


def test_cli_check_reports_the_live_tree():
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "check"], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout
    assert result.stdout.strip().endswith("test budget tree check: 0 problems")


def test_redaction_grammar_pin_in_the_tree(tmp_path):
    """Task #2441: a pinned grammar module may not grow, and a shrink lowers its pin."""
    root = _tree(tmp_path, {"src/grown.py": 4, "src/shrunk.py": 2, "src/same.py": 3})
    pins = {"src/grown.py": 3, "src/shrunk.py": 3, "src/same.py": 3, "src/gone.py": 3}
    problems = "\n".join(budget.check_tree(root, frozenset(), {}, pins))
    assert "src/grown.py: 4 lines, above its redaction grammar pin of 3" in problems
    assert "Redaction-Grammar: <evidence>" in problems
    assert "src/shrunk.py shrank to 2 lines" in problems
    assert "src/gone.py no longer exists" in problems
    assert "src/same.py" not in problems
    assert set(budget.GRAMMAR_PINS) >= {"src/unified_agent/telemetry/jwk.py"}


def test_redaction_grammar_growth_needs_the_trailer(tmp_path):
    repo = _repo(tmp_path)
    module = "src/unified_agent/telemetry/jwk.py"
    base = _commit(repo, "base", {module: "x = 1\n" * 3})
    _commit(repo, "fix: one more syntax", {module: "x = 1\n" * 4})
    grown = _count(repo, base)
    assert grown.returncode == 1 and "GRAMMAR GROWTH" in grown.stdout
    assert f"{module} 3 -> 4 lines" in grown.stdout
    _commit(repo, "fix: cited\n\nRedaction-Grammar: leak in hub log 2026-11-09 (task #9999)")
    messages = tmp_path / "messages"
    messages.write_bytes(_git(repo, "log", "-z", "--format=%B", f"{base}..HEAD").encode())
    allowed = _count(repo, base, "HEAD", "--messages", str(messages))
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert "allowed by Redaction-Grammar: leak in hub log" in allowed.stdout
    shrunk = _commit(repo, "refactor", {module: "x = 1\n"})
    assert _count(repo, base, shrunk).returncode == 0
