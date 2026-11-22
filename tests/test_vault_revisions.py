"""Revision-aware vault writes (task #2111, epic #2107 cloud plan Phase 3).

``vault_read`` returns ``revision`` (``sha256:<hex>`` of the note's bytes on
disk) and ``vault_write(expected_revision=...)`` refuses to write when the note
has changed since that read, so two agents editing one note cannot silently
overwrite each other.
"""

import hashlib
import threading

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import gateway, server
from unified_agent.tools.vault import (
    REVISION_ABSENT,
    VaultRevisionConflict,
    vault_read,
    vault_write,
)

NOTE = "Wiki/shared.md"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    for d in ("00 - Inbox", "Wiki", "Logs", "Templates"):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(root))
    monkeypatch.delenv("UA_VAULT_REQUIRE_REVISION", raising=False)
    return root


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _seed(vault, body: bytes = b"# Shared\n\nfirst\n", rel: str = NOTE):
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


class TestReadRevision:
    def test_revision_is_sha256_of_file_bytes(self, vault):
        body = b"# Shared\r\n\r\nline with CRLF and \xc3\xa9\n"
        _seed(vault, body)
        assert vault_read(NOTE)["revision"] == _sha(body)

    def test_revision_ignores_paging(self, vault):
        body = b"".join(f"line {i}\n".encode() for i in range(50))
        _seed(vault, body)
        assert vault_read(NOTE, offset=10, limit=5)["revision"] == _sha(body)

    def test_revision_is_over_raw_bytes_not_expanded_embeds(self, vault):
        (vault / "Wiki" / "embedded.md").write_text("embedded body\n")
        body = b"# Host\n\n![[embedded]]\n"
        _seed(vault, body)
        result = vault_read(NOTE, expand_embeds=True)
        assert "embedded body" in result["content"]
        assert result["revision"] == _sha(body)

    def test_absent_sentinel_value(self):
        assert REVISION_ABSENT == "absent"


class TestExpectedRevision:
    def test_matching_revision_writes_and_returns_new_revision(self, vault):
        target = _seed(vault)
        rev = vault_read(NOTE)["revision"]
        result = vault_write(NOTE, "second\n", expected_revision=rev)
        assert target.read_bytes() == b"second\n"
        assert result["revision"] == _sha(b"second\n")
        assert vault_read(NOTE)["revision"] == result["revision"]

    def test_stale_revision_is_refused_and_file_unchanged(self, vault):
        target = _seed(vault)
        stale = vault_read(NOTE)["revision"]
        target.write_bytes(b"edited elsewhere\n")
        current = _sha(b"edited elsewhere\n")
        with pytest.raises(VaultRevisionConflict) as exc:
            vault_write(NOTE, "clobber\n", expected_revision=stale)
        message = str(exc.value)
        assert current in message
        assert "re-read" in message and "merge" in message
        assert target.read_bytes() == b"edited elsewhere\n"

    def test_conflict_is_a_tool_error(self):
        assert issubclass(VaultRevisionConflict, ToolError)

    def test_absent_creates_new_note(self, vault):
        result = vault_write("Wiki/new.md", "fresh\n", expected_revision=REVISION_ABSENT)
        assert (vault / "Wiki" / "new.md").read_bytes() == b"fresh\n"
        assert result["revision"] == _sha(b"fresh\n")

    def test_absent_refused_when_note_exists(self, vault):
        target = _seed(vault)
        with pytest.raises(VaultRevisionConflict, match="already exists"):
            vault_write(NOTE, "clobber\n", expected_revision=REVISION_ABSENT)
        assert target.read_bytes() == b"# Shared\n\nfirst\n"

    def test_revision_for_missing_note_is_refused(self, vault):
        with pytest.raises(VaultRevisionConflict, match="does not exist"):
            vault_write("Wiki/gone.md", "x\n", expected_revision=_sha(b"anything"))
        assert not (vault / "Wiki" / "gone.md").exists()

    def test_append_with_matching_revision(self, vault):
        target = _seed(vault, b"a\n")
        rev = vault_read(NOTE)["revision"]
        result = vault_write(NOTE, "b\n", mode="append", expected_revision=rev)
        assert target.read_bytes() == b"a\nb\n"
        assert result["revision"] == _sha(b"a\nb\n")

    def test_append_with_stale_revision_is_refused(self, vault):
        target = _seed(vault, b"a\n")
        rev = vault_read(NOTE)["revision"]
        target.write_bytes(b"a\nz\n")
        with pytest.raises(VaultRevisionConflict):
            vault_write(NOTE, "b\n", append=True, expected_revision=rev)
        assert target.read_bytes() == b"a\nz\n"

    def test_template_write_checks_revision(self, vault):
        (vault / "Templates" / "t.md").write_text("# {{title}}\n\n{{content}}\n")
        target = _seed(vault, b"old\n")
        stale = _sha(b"not the current bytes")
        with pytest.raises(VaultRevisionConflict):
            vault_write(NOTE, "body", template="Templates/t.md", expected_revision=stale)
        assert target.read_bytes() == b"old\n"
        rev = vault_read(NOTE)["revision"]
        result = vault_write(NOTE, "body", template="Templates/t.md", expected_revision=rev)
        assert target.read_bytes() == b"# shared\n\nbody\n"
        assert result["revision"] == _sha(b"# shared\n\nbody\n")

    def test_malformed_revision_is_refused(self, vault):
        target = _seed(vault)
        for bad in ("", "sha256:xyz", "deadbeef", "md5:" + "0" * 32):
            with pytest.raises(ToolError, match="expected_revision"):
                vault_write(NOTE, "x\n", expected_revision=bad)
        assert target.read_bytes() == b"# Shared\n\nfirst\n"

    def test_omitting_revision_keeps_blind_overwrite(self, vault):
        target = _seed(vault)
        result = vault_write(NOTE, "blind\n")
        assert target.read_bytes() == b"blind\n"
        assert result["revision"] == _sha(b"blind\n")
        assert result["mode"] == "overwrite"

    def test_append_without_revision_returns_revision(self, vault):
        _seed(vault, b"a\n")
        result = vault_write(NOTE, "b\n", mode="append")
        assert result["revision"] == _sha(b"a\nb\n")


class TestLockedGuardStillWins:
    def test_locked_note_refused_even_with_matching_revision(self, vault):
        body = b"---\nlocked: true\n---\n\nbody\n"
        target = _seed(vault, body)
        rev = vault_read(NOTE)["revision"]
        with pytest.raises(ToolError, match="locked"):
            vault_write(NOTE, "clobber\n", expected_revision=rev)
        assert target.read_bytes() == body

    def test_locked_note_reports_lock_before_stale_revision(self, vault):
        body = b"---\nlocked: true\n---\n\nbody\n"
        _seed(vault, body)
        with pytest.raises(ToolError, match="locked") as exc:
            vault_write(NOTE, "clobber\n", expected_revision=_sha(b"stale"))
        assert not isinstance(exc.value, VaultRevisionConflict)


class TestRequireRevisionSetting:
    @pytest.fixture(autouse=True)
    def _require(self, vault, monkeypatch):
        monkeypatch.setenv("UA_VAULT_REQUIRE_REVISION", "1")

    def test_blind_overwrite_of_existing_note_refused(self, vault):
        target = _seed(vault)
        with pytest.raises(ToolError, match="UA_VAULT_REQUIRE_REVISION") as exc:
            vault_write(NOTE, "blind\n")
        assert "vault_read" in str(exc.value)
        assert target.read_bytes() == b"# Shared\n\nfirst\n"

    def test_create_without_revision_allowed(self, vault):
        vault_write("Wiki/brand-new.md", "new\n")
        assert (vault / "Wiki" / "brand-new.md").read_bytes() == b"new\n"

    def test_append_without_revision_allowed(self, vault):
        target = _seed(vault, b"a\n")
        vault_write(NOTE, "b\n", append=True)
        assert target.read_bytes() == b"a\nb\n"

    def test_overwrite_with_revision_allowed(self, vault):
        target = _seed(vault)
        rev = vault_read(NOTE)["revision"]
        vault_write(NOTE, "careful\n", expected_revision=rev)
        assert target.read_bytes() == b"careful\n"

    def test_off_by_default(self, vault, monkeypatch):
        monkeypatch.delenv("UA_VAULT_REQUIRE_REVISION")
        target = _seed(vault)
        vault_write(NOTE, "blind\n")
        assert target.read_bytes() == b"blind\n"


class TestTwoAgents:
    def test_second_writer_on_same_revision_is_refused(self, vault):
        target = _seed(vault)
        agent_a = vault_read(NOTE)
        agent_b = vault_read(NOTE)
        assert agent_a["revision"] == agent_b["revision"]

        first = vault_write(NOTE, "from A\n", expected_revision=agent_a["revision"])
        with pytest.raises(VaultRevisionConflict) as exc:
            vault_write(NOTE, "from B\n", expected_revision=agent_b["revision"])
        assert first["revision"] in str(exc.value)
        assert target.read_bytes() == b"from A\n"

        # B re-reads, merges and retries against the new revision.
        reread = vault_read(NOTE)
        assert reread["revision"] == first["revision"]
        vault_write(NOTE, "from A\nfrom B\n", expected_revision=reread["revision"])
        assert target.read_bytes() == b"from A\nfrom B\n"

    def test_concurrent_writers_exactly_one_wins(self, vault):
        target = _seed(vault)
        rev = vault_read(NOTE)["revision"]
        barrier = threading.Barrier(2)
        outcomes: dict[str, object] = {}

        def write(name: str) -> None:
            barrier.wait()
            try:
                outcomes[name] = vault_write(NOTE, f"from {name}\n", expected_revision=rev)
            except VaultRevisionConflict as exc:
                outcomes[name] = exc

        threads = [threading.Thread(target=write, args=(n,)) for n in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        wins = [n for n, o in outcomes.items() if isinstance(o, dict)]
        losses = [n for n, o in outcomes.items() if isinstance(o, VaultRevisionConflict)]
        assert len(wins) == 1 and len(losses) == 1
        assert target.read_bytes() == f"from {wins[0]}\n".encode()


class TestSurfaces:
    def test_mcp_schema_declares_expected_revision(self):
        tool = next(t for t in server.mcp._tool_manager.list_tools() if t.name == "vault_write")
        assert "expected_revision" in tool.parameters["properties"]
        assert "expected_revision" not in tool.parameters.get("required", [])

    def test_gateway_summary_keeps_expected_revision_readable(self):
        readable, hashed = gateway._ARGS_SUMMARY_FIELDS["vault_write"]
        assert "expected_revision" in readable
        assert "expected_revision" not in hashed
