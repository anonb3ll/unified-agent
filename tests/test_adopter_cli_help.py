"""CLI discovery and parse errors must not initialize or migrate hub state."""

import pytest

from unified_agent.tools import work_product_cli as cli


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    def forbidden():
        raise AssertionError("help/parse errors must not initialize storage")

    monkeypatch.setattr(cli.storage, "init_db", forbidden)


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_work_product_root_help_is_successful_without_storage(flag, capsys):
    assert cli.run_work_product_cli([flag]) == 0
    assert "work-product" in capsys.readouterr().out


@pytest.mark.parametrize(
    "command", ["submit", "release", "verify", "list", "evidence", "diff", "packs"]
)
@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_work_product_subcommand_help_does_not_initialize_storage(command, flag):
    with pytest.raises(SystemExit) as result:
        cli.run_work_product_cli([command, flag])
    assert result.value.code == 0


@pytest.mark.parametrize(
    "args", [["verify"], ["verify", "not-an-integer"], ["list", "--limit", "bad"]]
)
def test_work_product_parse_errors_do_not_initialize_storage(args):
    with pytest.raises(SystemExit) as result:
        cli.run_work_product_cli(args)
    assert result.value.code == 2


@pytest.mark.parametrize("args", [[], ["unknown-command"]])
def test_unknown_or_missing_command_preserves_usage_failure_without_storage(args):
    assert cli.run_work_product_cli(args) == 2
