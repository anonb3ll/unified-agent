"""Local work-product commands register post-init hooks before storage init.

Help and parse errors must not touch storage (test_adopter_cli_help.py), but
once a local command does initialize storage it must run the same start-up as
every other entry point: post-init hooks first, then ``init_db()`` (#1720).
"""

from __future__ import annotations

import pytest

from unified_agent import storage
from unified_agent.tools import work_product_cli as cli


@pytest.fixture
def init_calls(monkeypatch):
    """Empty hook registry; record the registered hook names at each init_db()."""
    monkeypatch.setattr(storage, "_post_init_hooks", {})
    seen: list[list[str]] = []
    monkeypatch.setattr(
        cli.storage, "init_db", lambda: seen.append([h.name for h in storage.post_init_hooks()])
    )
    monkeypatch.setattr(cli, "work_product_verify", lambda _id: {"tampered": False})
    monkeypatch.setattr(cli, "list_work_products", lambda **_kw: [])
    monkeypatch.setattr(
        cli,
        "work_product_evidence",
        lambda _id: {"ledger": {"chain_valid": True}, "consistent": True},
    )
    monkeypatch.setattr(cli, "work_product_diff", lambda _id: {"diffable": False, "reason": "x"})
    monkeypatch.setattr(cli, "work_product_submit", lambda *_a, **_kw: {"id": 1})
    return seen


@pytest.mark.parametrize(
    "argv",
    [
        ["verify", "1"],
        ["list"],
        ["evidence", "1"],
        ["diff", "1"],
        ["submit", "doc.md", "--pack", "legal"],
    ],
    ids=["verify", "list", "evidence", "diff", "submit"],
)
def test_local_command_registers_post_init_hooks_before_init_db(argv, init_calls, capsys):
    assert cli.run_work_product_cli(argv) == 0
    capsys.readouterr()
    assert len(init_calls) == 1, "the command must initialize storage exactly once"
    registered = init_calls[0]
    assert registered, "post-init hooks were not registered before storage.init_db()"
    assert "task_search.task_fts" in registered
