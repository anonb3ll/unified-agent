"""One lock detector for every vault path: spelling variants of `locked` (#1817)."""

import os
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import safety, wiki
from unified_agent.tools import obsidian, vault
from unified_agent.wiki import run_custodian

BOM = "﻿"

# Spellings that must read as locked.
LOCKED_VARIANTS = {
    "plain": "---\nlocked: true\n---\nbody\n",
    "crlf": "---\r\nlocked: true\r\n---\r\nbody\r\n",
    "bom": f"{BOM}---\nlocked: true\n---\nbody\n",
    "bom_crlf": f"{BOM}---\r\nlocked: true\r\n---\r\nbody\r\n",
    "yes": "---\nlocked: yes\n---\nbody\n",
    "on": "---\nlocked: on\n---\nbody\n",
    "capital_true": "---\nlocked: True\n---\nbody\n",
    "upper_true": "---\nlocked: TRUE\n---\nbody\n",
    "inline_comment": "---\nlocked: true # keep\n---\nbody\n",
    "double_quoted_key": '---\n"locked": true\n---\nbody\n',
    "single_quoted_key": "---\n'locked': true\n---\nbody\n",
    "quoted_value": '---\nlocked: "true"\n---\nbody\n',
    "after_other_keys": "---\ntitle: x\ntags: [a]\nlocked: true\n---\nbody\n",
    "dots_end_marker": "---\nlocked: true\n...\nbody\n",
    "unparseable_with_locked_key": "---\nlocked: true\nbad: [unclosed\n---\nbody\n",
}

# Spellings that must NOT read as locked.
UNLOCKED_VARIANTS = {
    "no_frontmatter": "just a body\n",
    "other_keys": "---\ntitle: x\n---\nbody\n",
    "false": "---\nlocked: false\n---\nbody\n",
    "no": "---\nlocked: no\n---\nbody\n",
    "off": "---\nlocked: off\n---\nbody\n",
    "false_crlf": "---\r\nlocked: false\r\n---\r\nbody\r\n",
    "body_mentions_lock": "---\ntitle: x\n---\nlocked: true\n",
    "unparseable_without_locked_key": "---\ntitle: x\nbad: [unclosed\n---\nbody\n",
}


def _write(path, text):
    path.write_bytes(text.encode("utf-8"))


@pytest.fixture
def root(tmp_path, monkeypatch):
    base = tmp_path / "vault"
    for d in ("Wiki", "Logs", "00 - Inbox"):
        (base / d).mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(base))
    monkeypatch.setenv("UA_OBSIDIAN_API_URL", "https://127.0.0.1:27124")
    monkeypatch.setenv("UA_OBSIDIAN_API_KEY", "test-key")
    vault._frontmatter_cache.clear()
    return base


@pytest.fixture
def plugin(monkeypatch):
    request = MagicMock()
    response = MagicMock(status_code=200, headers={}, text="")
    response.json.return_value = {"ok": True}
    request.return_value = response
    client = MagicMock()
    client.__enter__.return_value.request = request
    monkeypatch.setattr(obsidian.httpx, "Client", lambda **kwargs: client)
    return request


@pytest.mark.parametrize("name", LOCKED_VARIANTS)
def test_locked_variants_detected_by_local_helper(root, name):
    note = root / "Wiki" / "N.md"
    _write(note, LOCKED_VARIANTS[name])
    assert vault._is_locked_path(note) is True


@pytest.mark.parametrize("name", UNLOCKED_VARIANTS)
def test_unlocked_variants_not_detected_by_local_helper(root, name):
    note = root / "Wiki" / "N.md"
    _write(note, UNLOCKED_VARIANTS[name])
    assert vault._is_locked_path(note) is False


@pytest.mark.parametrize("name", LOCKED_VARIANTS)
def test_locked_variants_refused_by_cloud_path(root, name):
    note = root / "Wiki" / "N.md"
    _write(note, LOCKED_VARIANTS[name])
    with pytest.raises(ToolError):
        safety._check_cloud_vault_lock(note)


@pytest.mark.parametrize("name", ["false", "no", "off", "false_crlf", "other_keys"])
def test_unlocked_variants_pass_cloud_path(root, name):
    note = root / "Wiki" / "N.md"
    _write(note, UNLOCKED_VARIANTS[name])
    safety._check_cloud_vault_lock(note)


def test_cloud_path_still_refuses_unparseable_frontmatter_without_lock_key(root):
    note = root / "Wiki" / "N.md"
    _write(note, UNLOCKED_VARIANTS["unparseable_without_locked_key"])
    with pytest.raises(ToolError, match="malformed frontmatter"):
        safety._check_cloud_vault_lock(note)


def test_local_and_cloud_share_one_detector(root):
    """Every variant the cloud path calls locked, the local path calls locked too."""
    note = root / "Wiki" / "N.md"
    for text in LOCKED_VARIANTS.values():
        _write(note, text)
        vault._frontmatter_cache.clear()
        state, _ = safety.vault_lock_state(note.read_bytes())
        assert state in {safety.LOCK_LOCKED, safety.LOCK_UNVERIFIABLE}
        assert vault._is_locked_path(note) is True


@pytest.mark.parametrize("name", LOCKED_VARIANTS)
@pytest.mark.parametrize("mode", ["append", "overwrite"])
def test_vault_write_refuses_locked_variants(root, name, mode):
    note = root / "Wiki" / "N.md"
    _write(note, LOCKED_VARIANTS[name])
    before = note.read_bytes()
    with pytest.raises(ToolError, match="locked"):
        vault.vault_write("Wiki/N.md", "new\n", mode=mode)
    assert note.read_bytes() == before


@pytest.mark.parametrize("mode", ["append", "overwrite"])
def test_vault_write_still_writes_unlocked_variants(root, mode):
    note = root / "Wiki" / "N.md"
    _write(note, UNLOCKED_VARIANTS["false_crlf"])
    vault.vault_write("Wiki/N.md", "new\n", mode=mode)
    assert "new" in note.read_text()


@pytest.mark.parametrize("name", LOCKED_VARIANTS)
def test_obsidian_append_refuses_locked_variants(root, plugin, name):
    _write(root / "Wiki" / "N.md", LOCKED_VARIANTS[name])
    with pytest.raises(ToolError, match="locked"):
        obsidian.obsidian_append("Wiki/N.md", "x")
    plugin.assert_not_called()


@pytest.mark.parametrize("name", ["false", "no", "off", "false_crlf"])
def test_obsidian_append_allows_unlocked_variants(root, plugin, name):
    _write(root / "Wiki" / "N.md", UNLOCKED_VARIANTS[name])
    obsidian.obsidian_append("Wiki/N.md", "x")
    assert plugin.call_count == 1


def test_oversized_frontmatter_past_small_head_still_locked(root):
    note = root / "Wiki" / "N.md"
    padding = "# " + "x" * (vault._FRONTMATTER_MAX_BYTES + 2000) + "\n"
    _write(note, "---\n" + padding + "locked: yes\n---\nbody\n")
    assert vault._is_locked_path(note) is True


# --- vault_journal -----------------------------------------------------------


def test_journal_refuses_a_locked_day_note(root):
    day = "2026-01-02"
    note = root / "Logs" / f"{day}.md"
    _write(note, "---\r\nlocked: true\r\n---\r\n# held\r\n")
    before = note.read_bytes()
    with pytest.raises(ToolError, match="locked"):
        vault.vault_journal("entry", agent="tester", date=day)
    assert note.read_bytes() == before


def test_journal_still_appends_to_unlocked_day_note(root):
    day = "2026-01-02"
    note = root / "Logs" / f"{day}.md"
    _write(note, "---\nlocked: false\n---\n# open\n")
    vault.vault_journal("entry", agent="tester", date=day)
    assert "entry" in note.read_text()


def test_journal_creates_a_new_day_note(root):
    result = vault.vault_journal("first", agent="tester", date="2026-01-03")
    assert result["created_note"] is True


# --- wiki custodian ----------------------------------------------------------


@pytest.mark.parametrize("name", ["plain", "crlf", "yes"])
def test_custodian_skips_a_locked_health_report(root, name):
    health = root / "Wiki" / "Health.md"
    _write(health, LOCKED_VARIANTS[name])
    before = health.read_bytes()
    with patch("unified_agent.config.vault_root", return_value=root):
        result = run_custodian(dry_run=False)
    assert result["index_rebuilt"] is False
    assert result["health_report_skipped"] == "locked"
    assert health.read_bytes() == before


def test_custodian_still_rewrites_an_unlocked_health_report(root):
    health = root / "Wiki" / "Health.md"
    _write(health, "---\nlocked: false\n---\nOLDMARKER\n")
    with patch("unified_agent.config.vault_root", return_value=root):
        result = run_custodian(dry_run=False)
    assert result["index_rebuilt"] is True
    assert "OLDMARKER" not in health.read_text()


# --- bounded-head regressions (re-review) --------------------------------------


def _keys(n_bytes):
    return "".join(f"k{i}: {i}\n" for i in range(n_bytes // 8))


@pytest.mark.parametrize("filler", [90 * 1024, 12 * 1024])
def test_dots_marker_before_a_lock_past_the_bound_is_not_unlocked(root, filler):
    """`...` ends the YAML early, but the `---` block runs on and carries the lock."""
    text = "---\na: 1\n...\n" + _keys(filler) + "locked: true\n---\nbody"
    note = root / "Wiki" / "N.md"
    _write(note, text)
    assert vault._is_locked(text) is True
    assert vault._is_locked_path(note) is True
    if filler > safety.VAULT_LOCK_HEAD_BYTES:
        with pytest.raises(ToolError, match="complete, bounded frontmatter"):
            safety._check_cloud_vault_lock(note)
    else:
        with pytest.raises(ToolError):
            safety._check_cloud_vault_lock(note)


@pytest.mark.parametrize("value", ["yes", "on", "True"])
def test_lock_after_dots_marker_inside_the_dash_block_is_honoured(root, value):
    note = root / "Wiki" / "N.md"
    _write(note, f"---\na: 1\n...\nlocked: {value}\n---\nbody\n")
    assert vault._is_locked_path(note) is True
    with pytest.raises(ToolError):
        safety._check_cloud_vault_lock(note)


def test_dots_marker_without_a_dash_block_stays_unlocked(root):
    note = root / "Wiki" / "N.md"
    _write(note, "---\na: 1\n...\nlocked: true\nbody only\n")
    assert vault._is_locked_path(note) is False


def test_frontmatter_past_the_first_widening_is_still_seen(root):
    note = root / "Wiki" / "N.md"
    _write(note, "---\n" + _keys(200 * 1024) + "locked: yes\n---\nbody\n")
    assert vault._is_locked_path(note) is True


def test_unterminated_frontmatter_past_the_read_ceiling_fails_closed(root):
    note = root / "Wiki" / "N.md"
    _write(note, "---\n" + _keys(vault._LOCK_READ_CEILING + 4096))
    assert vault._is_locked_path(note) is True


def test_cache_does_not_serve_a_replaced_file_with_restored_mtime(root):
    note = root / "Wiki" / "N.md"
    _write(note, "---\nlocked: yes\n---\nbody\n")
    stamp = note.stat().st_mtime_ns
    assert vault._is_locked_path(note) is True
    replacement = root / "Wiki" / "R.md"
    _write(replacement, "---\nlocked: no \n---\nbody\n")  # same size
    os.replace(replacement, note)
    os.utime(note, ns=(stamp, stamp))
    assert vault._is_locked_path(note) is False
    assert len(vault._frontmatter_cache[str(note)]) == 5


def test_body_line_is_not_mistaken_for_a_malformed_frontmatter_lock(root):
    closed = root / "Wiki" / "C.md"
    _write(closed, "---\nbad: [unclosed\n---\nLocked: the door\n")
    assert vault._is_locked_path(closed) is False
    unclosed = root / "Wiki" / "U.md"
    _write(unclosed, "---\ntitle: x\n\nLocked: the door\n")
    assert vault._is_locked_path(unclosed) is False
    # A lock key inside the malformed block itself still fails closed.
    inside = root / "Wiki" / "I.md"
    _write(inside, "---\nbad: [unclosed\nLocked: 1\n---\nbody\n")
    assert vault._is_locked_path(inside) is True


def test_deeply_nested_frontmatter_is_unverifiable_without_parsing(root, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("yaml.compose must not run on over-nested frontmatter")

    monkeypatch.setattr(safety.yaml, "compose", boom)
    deep = "---\nx: " + "[" * 2000 + "\n---\nbody\n"
    state, _ = safety.vault_lock_state(deep.encode())
    assert state == safety.LOCK_UNVERIFIABLE
    note = root / "Wiki" / "N.md"
    _write(note, deep)
    assert vault._is_locked_path(note) is False
    with pytest.raises(ToolError):
        safety._check_cloud_vault_lock(note)
    _write(note, "---\nLocked: yes\nx: " + "{" * 2000 + "\n---\nbody\n")
    assert vault._is_locked_path(note) is True


def test_cloud_lock_refusal_is_a_dedicated_error_type(root):
    note = root / "Wiki" / "N.md"
    _write(note, LOCKED_VARIANTS["yes"])
    with pytest.raises(safety.VaultLockedError):
        safety._check_cloud_vault_lock(note)


def test_custodian_skips_only_on_the_dedicated_lock_error(root, monkeypatch):
    def locked(*args, **kwargs):
        raise safety.VaultLockedError("cloud vault access excludes locked notes")

    monkeypatch.setattr(wiki, "resolve_in_vault", locked)
    with patch("unified_agent.config.vault_root", return_value=root):
        result = run_custodian(dry_run=False)
    assert result["health_report_skipped"] == "locked"
    assert result["index_rebuilt"] is False

    def other(*args, **kwargs):
        raise ToolError("path is locked down by something else")  # same word, other error

    monkeypatch.setattr(wiki, "resolve_in_vault", other)
    with patch("unified_agent.config.vault_root", return_value=root), pytest.raises(ToolError):
        run_custodian(dry_run=False)
