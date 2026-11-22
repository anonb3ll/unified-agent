"""Cloud vault capability: approved paths are useful, excluded content stays closed.

These tests pin cloud_agent17 to the cloud-vault-steward profile, which remains a
policy-overlay downgrade. Its code default is developer (2026-11-11).
tests/test_cloud_Agent17_chief_of_staff.py covers that default.
"""

import builtins
import contextlib
import datetime
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, profiles
from unified_agent.config_revisions import _overlay_is_allowed
from unified_agent.safety import resolve_in_vault
from unified_agent.tools import vault


@pytest.fixture
def cloud_vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    for directory in ("Wiki", "00 - Inbox", "Logs", "01 - Projects", ".obsidian"):
        (root / directory).mkdir(parents=True)
    (root / "Wiki/Home.md").write_text("# Home\npublic breadcrumb\n")
    (root / "Wiki/Locked.md").write_text("---\nlocked: true\n---\nprivate breadcrumb\n")
    (root / "01 - Projects/Secret.md").write_text("private breadcrumb\n")
    monkeypatch.setenv("UA_VAULT_PATH", str(root))
    # Even an installation-wide write grant cannot broaden this caller.
    monkeypatch.setenv("UA_VAULT_WRITE_DIRS", "Wiki:Logs:01 - Projects")
    monkeypatch.setenv("UA_AGENT_WORKSPACES", "cloud_agent17:DENY")
    monkeypatch.setenv("UA_AGENT_LABELS", "cloud_agent17:server_agent2")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setitem(
        profiles.DEFAULT_PROFILES_BY_IDENTITY, "cloud_agent17", profiles.CLOUD_VAULT_STEWARD
    )
    identity = config.agent_identity_var.set("cloud_agent17")
    session = config.mcp_session_id_var.set("cloud-vault-scope-test")
    try:
        assert profiles.current() == profiles.CLOUD_VAULT_STEWARD
        yield root
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)
        profiles._active.pop("cloud-vault-scope-test", None)


def test_exact_manifest_and_denied_alternate_tools(cloud_vault):
    from unified_agent import server

    assert server.callable_tool_names() == profiles.CLOUD_VAULT_STEWARD_ALLOWED_TOOLS
    assert server.manifest_tool_names() == profiles.CLOUD_VAULT_STEWARD_ALLOWED_TOOLS
    assert len(server.callable_tool_names()) == 13
    assert len(profiles.CLOUD_ASSISTANT_ALLOWED_TOOLS) == 8
    for name in (
        "vault_move",
        "vault_archive_plan",
        "vault_to_memory_ingest",
        "vault_links",
        "vault_query",
        "vault_briefing_digest",
        "wiki_custodian",
        "obsidian_append",
        "obsidian_search",
        "fs_read",
        "fs_write",
        "run_cmd",
        "export_data",
        "agent_token_issue",
        "managed_secret_set",
        "config_revision_manage",
        "approval_resolve",
        "profile_activate",
        "profile_reset",
    ):
        assert not profiles.tool_is_allowed_for(profiles.CLOUD_VAULT_STEWARD, name)


@pytest.mark.parametrize("hosted_guard", ["off", "explicit", "container"])
def test_discovery_with_request_bound_workspace_deny(
    cloud_vault, tmp_path, monkeypatch, hosted_guard
):
    from unified_agent import hosted_capabilities
    from unified_agent.tools.files import fs_list
    from unified_agent.tools.shell import run_cmd

    workspace = tmp_path / "empty-workspace"
    workspace.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(workspace))
    monkeypatch.setenv("UA_HOSTED_WORKSPACE_GUARD", "1" if hosted_guard == "explicit" else "0")
    if hosted_guard == "container":
        monkeypatch.setattr(hosted_capabilities, "CONTAINER_WORKSPACE", workspace)
    binding = config.workspace_root_var.set(config.WORKSPACE_DENIED)
    try:
        assert [e["name"] for e in vault.vault_list("Wiki")["entries"]] == ["Home.md"]
        assert vault.vault_search("breadcrumb", "Wiki")["matches"] == [
            {"path": "Wiki/Home.md", "line_no": 2, "line": "public breadcrumb"}
        ]
        for tool, args in ((fs_list, (".",)), (run_cmd, ("echo hi",))):
            with pytest.raises(ToolError, match="filesystem access is denied"):
                tool(*args)
        with pytest.raises(ToolError, match="restricted to unlocked Wiki"):
            vault.vault_list("01 - Projects")
    finally:
        config.workspace_root_var.reset(binding)


@pytest.mark.parametrize("vault_state", ["empty", "missing", "disabled"])
def test_hosted_vault_guard_stays_fail_closed_under_workspace_deny(
    cloud_vault, tmp_path, monkeypatch, vault_state
):
    workspace = tmp_path / "empty-workspace"
    workspace.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(workspace))
    monkeypatch.setenv("UA_HOSTED_WORKSPACE_GUARD", "1")
    if vault_state == "disabled":
        monkeypatch.setenv("UA_VAULT_PATH", "")
    else:
        root = tmp_path / "unpopulated-vault"
        if vault_state == "empty":
            root.mkdir()
        monkeypatch.setenv("UA_VAULT_PATH", str(root))
    binding = config.workspace_root_var.set(config.WORKSPACE_DENIED)
    try:
        for tool, args in ((vault.vault_list, ("Wiki",)), (vault.vault_search, ("note", "Wiki"))):
            with pytest.raises(ToolError, match="vault") as raised:
                tool(*args)
            assert "filesystem access is denied" not in str(raised.value)
    finally:
        config.workspace_root_var.reset(binding)


@pytest.mark.anyio
async def test_mcp_adapter_enforces_scope_and_rejects_internal_options(cloud_vault):
    from mcp.client import Client

    from unified_agent import server

    async with Client(server.mcp) as client:
        names = {tool.name for tool in (await client.session.list_tools()).tools}
        assert names == profiles.CLOUD_VAULT_STEWARD_ALLOWED_TOOLS
        allowed = await client.session.call_tool("vault_read", {"path": "Wiki/Home.md"})
        assert not allowed.is_error, allowed.content
        for name, arguments in (
            ("vault_read", {"path": "Wiki/Locked.md"}),
            ("vault_read", {"path": "01 - Projects/Secret.md"}),
            ("vault_write", {"path": "Logs/2026-10-02.md", "content": "clobber"}),
            ("vault_write", {"path": "Wiki/New.md", "content": "test", "for_journal": True}),
            ("vault_move", {"source": "Wiki/Home.md", "destination": "Wiki/New.md"}),
            ("profile_activate", {"profile": profiles.CHIEF_OF_STAFF}),
            ("observability_manage", {"action": "heartbeat", "params": {"agent": "cloud_agent17"}}),
        ):
            denied = await client.session.call_tool(name, arguments)
            assert denied.is_error, (name, denied.content)
    assert not (cloud_vault / "Wiki/New.md").exists()
    assert list((cloud_vault / "Logs").iterdir()) == []


def test_governed_non_mcp_tool_surface_cannot_bypass_profile(cloud_vault):
    from unified_agent import tool_surface

    with pytest.raises(ToolError, match="not exposed"):
        tool_surface.registry()["vault_move"]("Wiki/Home.md", "Wiki/Moved.md")
    with pytest.raises(ToolError, match="restricted"):
        tool_surface.registry()["vault_read"]("01 - Projects/Secret.md")
    assert (cloud_vault / "Wiki/Home.md").exists()


def test_unlocked_wiki_read_search_list_write_and_template(cloud_vault):
    assert "public breadcrumb" in vault.vault_read("Wiki/Home.md")["content"]
    assert {entry["name"] for entry in vault.vault_list()["entries"]} == {"Home.md"}
    matches = vault.vault_search("breadcrumb")["matches"]
    assert [match["path"] for match in matches] == ["Wiki/Home.md"]
    vault.vault_write("Wiki/Template.md", "# {{title}}\n{{content}}")
    result = vault.vault_write("Wiki/New.md", "created", template="Wiki/Template.md")
    assert result["path"] == "Wiki/New.md"
    vault.vault_write("Wiki/New.md", "\nappended", mode="append")
    assert (cloud_vault / "Wiki/New.md").read_text() == "# New\ncreated\nappended"


@pytest.mark.parametrize(
    "path",
    [
        "01 - Projects/Secret.md",
        "02 - Areas/nope.md",
        "03 - Resources/nope.md",
        "04 - Archive/nope.md",
        "00 - Inbox/nope.md",
        "Logs/2026-10-02.md",
        "AGENTS.md",
        "Wiki/../01 - Projects/Secret.md",
        "../outside.md",
        ".obsidian/nope.md",
        "Wiki/.hidden.md",
        "Wiki/code.py",
    ],
)
def test_read_and_write_cannot_escape_approved_scope(cloud_vault, path):
    with pytest.raises(ToolError):
        vault.vault_read(path)
    with pytest.raises(ToolError):
        vault.vault_write(path, "must not write", mode="append")


def test_absolute_paths_and_symlinks_obey_canonical_scope(cloud_vault, tmp_path):
    assert vault.vault_read(str(cloud_vault / "Wiki/Home.md"))["path"] == "Wiki/Home.md"
    outside = tmp_path / "outside.md"
    outside.write_text("outside secret")
    for name, target in (
        ("ParaAlias.md", cloud_vault / "01 - Projects/Secret.md"),
        ("LockAlias.md", cloud_vault / "Wiki/Locked.md"),
        ("Outside.md", outside),
    ):
        (cloud_vault / "Wiki" / name).symlink_to(target)
        with pytest.raises(ToolError):
            vault.vault_read(f"Wiki/{name}")
        with pytest.raises(ToolError):
            vault.vault_write(f"Wiki/{name}", "clobber")
    assert {e["name"] for e in vault.vault_list()["entries"]} == {"Home.md"}
    assert outside.read_text() == "outside secret"


@pytest.mark.parametrize(
    "header",
    [
        "locked: true",
        '"locked": "true" # comment',
        "LOCKED: YES",
        "locked: false\nlocked: true",
        "locked: true\nlocked: false",
        "locked: [false]",
        "locked: null",
        "locked: maybe",
        "defaults: &defaults {locked: true}\n<<: *defaults",
        "locked: [",
    ],
)
def test_lock_spellings_and_ambiguous_yaml_fail_closed(cloud_vault, header):
    original = f"---\r\n{header}\r\n---\r\nprivate breadcrumb\r\n"
    note = cloud_vault / "Wiki/Protected.md"
    note.write_text(original)
    for operation in (
        lambda: vault.vault_read("Wiki/Protected.md"),
        lambda: vault.vault_write("Wiki/Protected.md", "clobber", force=True),
        lambda: vault.vault_write("Wiki/Protected.md", "append", append=True),
        lambda: vault.vault_write("Wiki/New.md", template="Wiki/Protected.md"),
    ):
        with pytest.raises(ToolError):
            operation()
    assert "Protected.md" not in {e["name"] for e in vault.vault_list()["entries"]}
    assert not vault.vault_search("private breadcrumb")["matches"]
    assert note.read_text() == original.replace("\r\n", "\n")


def test_force_is_denied_even_on_unlocked_note(cloud_vault):
    with pytest.raises(ToolError, match="force"):
        vault.vault_write("Wiki/Home.md", "clobber", force=True)
    assert "public breadcrumb" in (cloud_vault / "Wiki/Home.md").read_text()


@pytest.mark.parametrize("header", ["locked: false # comment", '"locked": "false"', "locked: OFF"])
def test_explicitly_unlocked_yaml_remains_usable(cloud_vault, header):
    note = cloud_vault / "Wiki/Home.md"
    note.write_text(f"\ufeff---\n{header}\n---\nallowed content\n")
    assert "allowed content" in vault.vault_read("Wiki/Home.md")["content"]
    vault.vault_write("Wiki/Home.md", "edited")
    assert note.read_text() == "edited"


def test_template_and_embeds_cannot_import_para_or_locked_content(cloud_vault):
    with pytest.raises(ToolError):
        vault.vault_write("Wiki/New.md", template="01 - Projects/Secret.md")
    (cloud_vault / "Wiki/Child.md").write_text("allowed child")
    (cloud_vault / "Wiki/Home.md").write_text(
        "![[01 - Projects/Secret]] ![[Secret]] ![[Locked]] ![[Child]]"
    )
    content = vault.vault_read("Wiki/Home.md", expand_embeds=True)["content"]
    assert "private breadcrumb" not in content
    assert "allowed child" in content
    assert "![[Secret]]" in content and "![[Locked]]" in content


def test_glob_cannot_reveal_hidden_or_locked_targets(cloud_vault):
    (cloud_vault / "Wiki/.hidden.md").write_text("private breadcrumb")
    assert {e["name"] for e in vault.vault_list(glob="**/*")["entries"]} == {"Home.md"}
    with pytest.raises(ToolError, match="parent traversal"):
        vault.vault_list(glob="../01 - Projects/*")


@pytest.mark.parametrize("glob", ["../*", "**/../*", "../01 - Projects/*", "/tmp/*"])
def test_invalid_glob_is_refused_before_enumeration(cloud_vault, monkeypatch, glob):
    def unexpected_glob(*args):
        raise AssertionError("unsafe glob reached filesystem enumeration")

    monkeypatch.setattr(Path, "glob", unexpected_glob)
    with pytest.raises(ToolError, match="relative glob"):
        vault.vault_list(glob=glob)


def test_parent_glob_cannot_expose_para_alias_names(cloud_vault):
    (cloud_vault / "01 - Projects/PrivateName.md").symlink_to(cloud_vault / "Wiki/Home.md")
    with pytest.raises(ToolError, match="parent traversal"):
        vault.vault_list(glob="../01 - Projects/*")


def test_native_journal_appends_without_exposing_or_overwriting_history(cloud_vault):
    day = datetime.date.today().isoformat()
    target = cloud_vault / f"Logs/{day}.md"
    target.write_text("existing historical journal\n")
    result = vault.vault_journal("approved activity", agent="cloud_agent17", tags=["task/1780"])
    assert not result["created_note"]
    assert target.read_text().startswith("existing historical journal\n")
    assert "[cloud_agent17] approved activity #task/1780" in target.read_text()
    with pytest.raises(ToolError):
        vault.vault_read(result["path"])
    with pytest.raises(ToolError):
        vault.vault_write(result["path"], "overwrite")
    with pytest.raises(ToolError):
        vault.vault_write(result["path"], "append", mode="append")


def test_native_journal_creates_note_and_refuses_forged_identity(cloud_vault):
    with pytest.raises(ToolError):
        vault.vault_journal("forged", agent="server_agent2")
    result = vault.vault_journal("first entry", agent="cloud_agent17")
    assert result["created_note"]
    assert (cloud_vault / result["path"]).read_text().startswith("---\ndate:")


@pytest.mark.parametrize("target_kind", ["locked", "wiki_symlink", "day_symlink"])
def test_journal_denies_locked_and_redirected_targets(cloud_vault, target_kind):
    day = datetime.date.today().isoformat()
    target = cloud_vault / f"Logs/{day}.md"
    if target_kind == "locked":
        target.write_text("---\nlocked: true\n---\nhistory")
    else:
        other = cloud_vault / (
            "Wiki/Home.md" if target_kind == "wiki_symlink" else "Logs/2000-01-01.md"
        )
        if not other.exists():
            other.write_text("history")
        target.symlink_to(other)
    original = target.read_text()
    with pytest.raises(ToolError):
        vault.vault_journal("must not append", agent="cloud_agent17")
    assert target.read_text() == original


def test_journal_rejects_multiline_tags_before_creating_note(cloud_vault):
    with pytest.raises(ToolError, match="single-line"):
        vault.vault_journal("entry", agent="cloud_agent17", tags=["tag\nforged history"])
    assert list((cloud_vault / "Logs").iterdir()) == []


def test_journal_creation_race_preserves_existing_history(cloud_vault, monkeypatch):
    target = cloud_vault / f"Logs/{datetime.date.today().isoformat()}.md"
    real_open = builtins.open
    original = "---\nlocked: true\n---\nconcurrent history\n"
    race_injected = False

    def raced_open(path, mode="r", *args, **kwargs):
        nonlocal race_injected
        # The locked journal helper uses a+ to read dedupe state before appending.
        if mode in {"a", "a+"} and (isinstance(path, int) or Path(path) == target):
            target.write_text(original)
            race_injected = True
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", raced_open)
    with pytest.raises(ToolError, match="locked"):
        vault.vault_journal("must not append", agent="cloud_agent17")
    assert race_injected, "test did not inject history at the journal append open"
    assert target.read_text() == original


def test_concurrent_first_journal_writes_preserve_both_entries(cloud_vault, monkeypatch):
    target = cloud_vault / f"Logs/{datetime.date.today().isoformat()}.md"
    header_buffered = threading.Event()
    second_started = threading.Event()
    release_header = threading.Event()
    real_open = builtins.open

    def paused_open(path, mode="r", *args, **kwargs):
        note = real_open(path, mode, *args, **kwargs)
        # Include append/read so this pause exercises the actual locked writer.
        if mode in {"a", "a+", "x"} and (isinstance(path, int) or Path(path) == target):
            real_write = note.write

            def paused_write(data):
                written = real_write(data)
                if data.startswith("---\n"):
                    header_buffered.set()
                    assert release_header.wait(5), "test did not release the buffered header"
                return written

            note.write = paused_write
        return note

    def write_second():
        second_started.set()
        return vault.vault_journal("second concurrent entry", agent="cloud_agent17")

    monkeypatch.setattr(builtins, "open", paused_open)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            copy_context().run, vault.vault_journal, "first concurrent entry", "cloud_agent17"
        )
        try:
            assert header_buffered.wait(5)
            second = pool.submit(copy_context().run, write_second)
            assert second_started.wait(5)
            # A negative window: a writer that bypassed the lock appends at once.
            time.sleep(0.05)
            assert not second.done(), "competing writer bypassed initialization lock"
        finally:
            release_header.set()
        results = [first.result(timeout=5), second.result(timeout=5)]
    text = target.read_text()
    assert text.count("type: journal") == 1
    assert text.count("first concurrent entry") == 1
    assert text.count("second concurrent entry") == 1
    assert sum(result["created_note"] for result in results) == 1


def test_journal_lock_is_shared_with_another_hub_process(cloud_vault, tmp_path):
    target = cloud_vault / f"Logs/{datetime.date.today().isoformat()}.md"
    started = tmp_path / "child-started"
    script = """
import sys
from pathlib import Path
from unified_agent import config
from unified_agent.tools.vault import vault_journal
config.agent_identity_var.set("cloud_agent17")
config.mcp_session_id_var.set("other-hub-test")
Path(sys.argv[1]).write_text("started")
vault_journal("other process entry", agent="cloud_agent17")
"""
    # The hub's per-note lock is a sidecar file, not the note's inode (#1890).
    with vault.note_write_lock(target), open(target, "a") as creator:
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(started)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert started.exists(), "child did not reach journal call"
            time.sleep(0.05)  # a negative window: a bypassing child writes at once
            assert child.poll() is None, "other process bypassed initialization lock"
            assert target.read_text() == ""
        finally:
            creator.write("existing creator header\n")
            creator.flush()
    try:
        stdout, stderr = child.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        raise
    assert child.returncode == 0, (stdout, stderr)
    assert target.read_text().startswith("existing creator header\n")
    assert target.read_text().count("other process entry") == 1


def test_template_processing_cannot_hide_a_new_target_lock(cloud_vault, monkeypatch):
    (cloud_vault / "Wiki/Template.md").write_text("{{content}}")
    target = cloud_vault / "Wiki/Home.md"
    original = "---\nlocked: true # added concurrently\n---\nprotected\n"

    def raced_template(*args):
        target.write_text(original)
        return "must not write"

    monkeypatch.setattr(vault, "_apply_template", raced_template)
    with pytest.raises(ToolError, match="locked"):
        vault.vault_write("Wiki/Home.md", template="Wiki/Template.md")
    assert target.read_text() == original


def test_unverifiable_lock_is_refused(cloud_vault, monkeypatch):
    real_open = builtins.open

    def denied_open(path, *args, **kwargs):
        if Path(path) == cloud_vault / "Wiki/Home.md":
            raise PermissionError("unreadable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied_open)
    with pytest.raises(ToolError, match="cannot verify"):
        vault.vault_write("Wiki/Home.md", "clobber")


def test_large_or_unterminated_frontmatter_is_not_treated_as_unlocked(cloud_vault):
    (cloud_vault / "Wiki/Home.md").write_text(
        "---\n# " + "padding" * 10000 + "\nlocked: true\n---\n"
    )
    with pytest.raises(ToolError, match="bounded frontmatter"):
        vault.vault_read("Wiki/Home.md")


def test_truncated_line_cannot_mimic_a_closing_frontmatter_delimiter(cloud_vault):
    # The bounded head ends with '---', but the actual line continues as a YAML
    # key. Treating that partial line as the closing delimiter would hide a lock.
    head = "---\n# " + "x" * (65536 - len("---\n# \n---")) + "\n---"
    assert len(head.encode()) == 65536
    (cloud_vault / "Wiki/Home.md").write_text(head + "key: value\nlocked: true\n---\nprivate")
    with pytest.raises(ToolError, match="bounded frontmatter"):
        vault.vault_read("Wiki/Home.md")
    with pytest.raises(ToolError):
        vault.vault_write("Wiki/Home.md", "clobber")


def test_lock_changes_take_effect_in_same_session(cloud_vault):
    vault.vault_read("Wiki/Home.md")
    (cloud_vault / "Wiki/Home.md").write_text("---\nlocked: true\n---\nnow private")
    with pytest.raises(ToolError, match="locked"):
        vault.vault_read("Wiki/Home.md")


def test_session_and_overlay_cannot_broaden_cloud_authority(cloud_vault):
    assert _overlay_is_allowed(profiles.CLOUD_ASSISTANT, profiles.CLOUD_VAULT_STEWARD)
    assert not _overlay_is_allowed(profiles.CLOUD_VAULT_STEWARD, profiles.CLOUD_ASSISTANT)
    for name in profiles.VALID_PROFILES - {profiles.CLOUD_VAULT_STEWARD}:
        with pytest.raises(ToolError):
            profiles.activate(name)
    session = config.mcp_session_id_var.set("reconnected-cloud-vault-test")
    try:
        assert profiles.current() == profiles.CLOUD_VAULT_STEWARD
        with pytest.raises(ToolError):
            resolve_in_vault("01 - Projects/Secret.md")
    finally:
        config.mcp_session_id_var.reset(session)


@pytest.mark.parametrize(
    "note",
    [
        # One multi-line YAML title, but the hub's line-based lock check (used by
        # every other vault path) reads `locked: true` and treats it as locked.
        '---\ntitle: "a\nlocked: true\nb"\n---\nprivate breadcrumb\n',
        # YAML ends at `...`; the hub's frontmatter block runs to the next `---`.
        "---\ntitle: a\n...\nlocked: true\n---\nprivate breadcrumb\n",
    ],
)
def test_hub_lock_semantics_also_close_cloud_access(cloud_vault, note):
    target = cloud_vault / "Wiki/Quoted.md"
    target.write_text(note)
    assert vault._is_locked_path(target)
    with pytest.raises(ToolError, match="locked"):
        vault.vault_read("Wiki/Quoted.md")
    with pytest.raises(ToolError, match="locked"):
        vault.vault_write("Wiki/Quoted.md", "append", append=True)
    assert "Quoted.md" not in {e["name"] for e in vault.vault_list()["entries"]}
    assert not vault.vault_search("private breadcrumb")["matches"]
    (cloud_vault / "Wiki/Home.md").write_text("![[Quoted]]")
    assert (
        "private breadcrumb" not in vault.vault_read("Wiki/Home.md", expand_embeds=True)["content"]
    )
    assert target.read_text() == note


def test_cloud_wiki_writes_stay_within_installation_write_dirs(cloud_vault, monkeypatch):
    monkeypatch.setenv("UA_VAULT_WRITE_DIRS", "Logs")
    with pytest.raises(ToolError, match="restricted"):
        vault.vault_write("Wiki/New.md", "must not land")
    assert not (cloud_vault / "Wiki/New.md").exists()
    # The installation narrowed Wiki to one subtree; cloud gets only that subtree.
    monkeypatch.setenv("UA_VAULT_WRITE_DIRS", "Wiki/Drafts/:Logs")
    with pytest.raises(ToolError, match="restricted"):
        vault.vault_write("Wiki/New.md", "must not land")
    vault.vault_write("Wiki/Drafts/New.md", "allowed")
    assert (cloud_vault / "Wiki/Drafts/New.md").read_text() == "allowed"


def test_cloud_journal_requires_installation_logs_write_dir(cloud_vault, monkeypatch):
    monkeypatch.setenv("UA_VAULT_WRITE_DIRS", "Wiki")
    with pytest.raises(ToolError, match="restricted"):
        vault.vault_journal("must not land", agent="cloud_agent17")
    assert list((cloud_vault / "Logs").iterdir()) == []
    monkeypatch.setenv("UA_VAULT_WRITE_DIRS", "Wiki:Logs")
    assert vault.vault_journal("allowed", agent="cloud_agent17")["created_note"]


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_cloud_journal_accepts_adjacent_days(cloud_vault, offset):
    day = (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()
    result = vault.vault_journal("adjacent day", agent="cloud_agent17", date=day)
    assert result["path"] == f"Logs/{day}.md"


@pytest.mark.parametrize("offset", [-2, 2, -2500])
def test_cloud_journal_refuses_distant_days(cloud_vault, offset):
    day = (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()
    with pytest.raises(ToolError, match="within one day"):
        vault.vault_journal("rewrite history", agent="cloud_agent17", date=day)
    with pytest.raises(ToolError, match="within one day"):
        vault.vault_journal("rewrite history", agent="cloud_agent17", date="2019-01-01")
    assert list((cloud_vault / "Logs").iterdir()) == []


@pytest.mark.parametrize("replace_parent", [False, True])
def test_cloud_journal_refuses_symlink_swap_while_waiting_for_lock(
    cloud_vault, tmp_path, monkeypatch, replace_parent
):
    target = cloud_vault / f"Logs/{datetime.date.today().isoformat()}.md"
    target.write_text("existing history\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_note = outside / target.name
    real_lock = vault.note_write_lock

    @contextlib.contextmanager
    def raced_lock(path):
        with real_lock(path):
            if replace_parent:
                target.parent.rename(cloud_vault / "saved-logs")
                target.parent.symlink_to(outside, target_is_directory=True)
            else:
                target.unlink()
                target.symlink_to(outside_note)
            yield

    monkeypatch.setattr(vault, "note_write_lock", raced_lock)
    with pytest.raises(ToolError):
        vault.vault_journal("must not escape", agent="cloud_agent17")
    assert not outside_note.exists()


def test_journal_lock_check_uses_open_inode_after_path_replacement(cloud_vault, monkeypatch):
    target = cloud_vault / f"Logs/{datetime.date.today().isoformat()}.md"
    locked = "---\nlocked: true\n---\nprotected history\n"
    target.write_text(locked)
    real_open = builtins.open
    retained = target.with_name("retained.md")

    def replaced_after_open(path, mode="r", *args, **kwargs):
        note = real_open(path, mode, *args, **kwargs)
        if mode == "a+":
            target.rename(retained)
            target.write_text("unlocked replacement\n")
        return note

    # Initial authorization saw an unlocked note; a locked replacement arrives
    # immediately before the append open, then moves again before the lock read.
    target.write_text("unlocked original\n")
    real_lock = vault.note_write_lock

    @contextlib.contextmanager
    def raced_lock(path):
        with real_lock(path):
            target.write_text(locked)
            yield

    # Isolate the post-open descriptor guard from the earlier cloud resolver.
    real_resolve = vault.resolve_in_vault
    calls = 0

    def first_resolution_only(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_resolve(*args, **kwargs) if calls == 1 else target

    monkeypatch.setattr(vault, "resolve_in_vault", first_resolution_only)
    monkeypatch.setattr(vault, "note_write_lock", raced_lock)
    monkeypatch.setattr(builtins, "open", replaced_after_open)
    with pytest.raises(ToolError, match="locked"):
        vault.vault_journal("must not append", agent="cloud_agent17")
    assert retained.read_text() == locked
    assert target.read_text() == "unlocked replacement\n"
