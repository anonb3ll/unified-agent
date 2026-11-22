import pytest
from textual.css.query import WrongType
from textual.widgets import Button, DataTable, TextArea

from unified_agent import config, profiles, server
from unified_agent.tui import (
    DashboardApp,
    EditNotesScreen,
    _local_tool_registry,
    _parse_assignee,
)


def _press(button_id: str) -> Button.Pressed:
    """A Button.Pressed for `button_id`, for driving handlers without a real click."""
    return Button.Pressed(Button(id=button_id))


@pytest.fixture(autouse=True)
def _clean_profile_state():
    with profiles._lock:
        profiles._active.clear()
    yield
    with profiles._lock:
        profiles._active.clear()


@pytest.fixture
def authenticated_session(monkeypatch):
    """Same exposure gate MCP tools/list uses: auth + identity + session id."""
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set("mcp-session-tui")
    try:
        yield
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


def test_parse_assignee():
    assert _parse_assignee("claimed-by: Desktop_Agent1 @ 2026-07-12T00:00:00Z") == "Desktop_Agent1"
    assert _parse_assignee("CLAIMED-BY: Laptop_agent3") == "Laptop_agent3"
    assert _parse_assignee("some notes\nclaimed-by: Operator\nmore") == "Operator"
    assert _parse_assignee("no claim here") == "-"
    assert _parse_assignee("claimed-by: [waiting-for: dev-claim]") == "-"
    assert _parse_assignee("") == "-"
    assert _parse_assignee(None) == "-"


def test_local_tool_registry_matches_profile_filtered_registered_tools(authenticated_session):
    """TUI registry must equal the profile-filtered REGISTERED_TOOLS set (task #1113)."""
    expected = profiles.allowed_tools(set(server.REGISTERED_TOOLS))
    reg = _local_tool_registry()
    assert set(reg) == expected
    for name in expected:
        assert reg[name] is server.REGISTERED_TOOLS[name]


def test_local_tool_registry_picks_up_newly_registered_tool(monkeypatch, authenticated_session):
    """A tool added to REGISTERED_TOOLS appears in the TUI without a TUI-side edit."""

    def fake_tool_for_tui_registry(**_kwargs):
        return {"ok": True}

    monkeypatch.setitem(
        server.REGISTERED_TOOLS, "fake_tool_for_tui_registry", fake_tool_for_tui_registry
    )
    assert "fake_tool_for_tui_registry" in server.REGISTERED_TOOLS
    assert "fake_tool_for_tui_registry" in _local_tool_registry()


def test_local_tool_registry_excludes_profile_filtered_out_tools(
    monkeypatch, authenticated_session
):
    """A registered tool denied by the active profile must not appear in the TUI."""

    def secret_tool_not_on_research(**_kwargs):
        return {"ok": True}

    monkeypatch.setitem(
        server.REGISTERED_TOOLS, "secret_tool_not_on_research", secret_tool_not_on_research
    )
    profiles.activate("research")
    assert "secret_tool_not_on_research" in server.REGISTERED_TOOLS
    assert "secret_tool_not_on_research" not in profiles.allowed_tools(set(server.REGISTERED_TOOLS))
    assert "secret_tool_not_on_research" not in _local_tool_registry()


def test_local_tool_registry_keeps_previously_exposed_tools_callable():
    """Tools the TUI already dispatched remain present when exposure is inactive."""
    reg = _local_tool_registry()
    for name in (
        "task_list",
        "task_add",
        "task_update",
        "task_claim",
        "task_delete",
        "memory_search",
        "memory_get",
        "git_worktree_list",
        "vault_list",
        "vault_read",
        "system_doctor",
        "review_and_notify",
    ):
        assert callable(reg[name]), name
        assert reg[name].__name__ == name
    assert reg.get("definitely_not_a_tool") is None


@pytest.mark.anyio
async def test_tui_compose():
    app = DashboardApp()
    async with app.run_test() as _pilot:
        assert app.title.startswith("Unified Agent TUI Control Center")

        assert app.query_one("Header") is not None
        assert app.query_one("Footer") is not None
        assert app.query_one("TabbedContent") is not None

        # Verify tables and elements across tabs
        assert app.query_one("#services-table") is not None
        assert app.query_one("#doctor-table") is not None
        assert app.query_one("#tasks-table") is not None
        assert app.query_one("#journal-table") is not None
        assert app.query_one("#worktree-table") is not None
        assert app.query_one("#search-results") is not None
        assert app.query_one("#mcp-tool-select") is not None
        assert app.query_one("#logs-stream-log") is not None


@pytest.mark.anyio
async def test_pane_widget_skips_unmounted_but_surfaces_wrong_type():
    """Lazily-mounted panes are skipped; a mistyped selector still raises.

    Refresh workers start in on_mount and can fire before a TabbedContent pane
    has mounted its table, so a missing widget must be a no-op. A selector that
    resolves to the wrong widget type is a bug and must not be swallowed.
    """
    app = DashboardApp()
    async with app.run_test() as _pilot:
        assert app._pane_widget("#definitely-not-mounted", DataTable) is None

        with pytest.raises(WrongType):
            app._pane_widget("#logs-stream-log", DataTable)


@pytest.mark.anyio
async def test_refresh_tolerates_removed_table():
    """A refresh whose table has gone away returns quietly instead of crashing."""
    app = DashboardApp()
    async with app.run_test() as _pilot:
        await app.query_one("#worktree-table", DataTable).remove()

        await app.refresh_worktrees()  # must not raise


@pytest.mark.anyio
async def test_setup_tables_uses_declared_columns():
    """Every declared table gets its headers from TABLE_COLUMNS, once."""
    app = DashboardApp()
    async with app.run_test() as _pilot:
        for selector, columns in DashboardApp.TABLE_COLUMNS.items():
            table = app.query_one(selector, DataTable)
            labels = [str(col.label) for col in table.columns.values()]
            assert labels == list(columns), selector


@pytest.mark.anyio
async def test_edit_notes_button_exists():
    """The task detail pane exposes a notes editor, not just status controls."""
    app = DashboardApp()
    async with app.run_test() as _pilot:
        assert app.query_one("#btn-edit-notes") is not None


@pytest.mark.anyio
async def test_edit_notes_screen_prefills_current_notes():
    """The editor opens populated, so saving never silently truncates existing notes."""
    app = DashboardApp()
    async with app.run_test() as pilot:
        app.push_screen(EditNotesScreen(42, "original notes"))
        await pilot.pause()

        assert app.screen.query_one("#notes-edit-input", TextArea).text == "original notes"


@pytest.mark.anyio
async def test_edit_notes_save_returns_text_cancel_returns_none(monkeypatch):
    """Save yields the edited text; cancel yields None.

    The distinction matters: clearing the notes is a legitimate edit, so an empty
    string must not be confused with having cancelled the dialog.
    """
    app = DashboardApp()
    async with app.run_test() as pilot:
        screen = EditNotesScreen(42, "original notes")
        app.push_screen(screen)
        await pilot.pause()

        captured = []
        monkeypatch.setattr(screen, "dismiss", lambda value=None: captured.append(value))

        screen.query_one("#notes-edit-input", TextArea).text = ""
        screen.on_button_pressed(_press("notes-edit-save"))
        assert captured == [""], "an emptied editor must save as empty, not as cancelled"

        screen.on_button_pressed(_press("notes-edit-cancel"))
        assert captured == ["", None]


@pytest.mark.anyio
async def test_edit_notes_is_a_no_op_with_nothing_selected():
    """With no task selected the action returns quietly rather than opening a modal."""
    app = DashboardApp()
    async with app.run_test() as _pilot:
        app.selected_task_id = None
        depth = len(app.screen_stack)

        app.edit_selected_task_notes()  # must not raise

        assert len(app.screen_stack) == depth


@pytest.mark.anyio
async def test_claim_reports_safe_wait_instead_of_silently_refreshing():
    """safe_wait is a returned dict, so the claim action has no exception to show (#2388)."""
    app = DashboardApp()
    app.selected_task_id = 5
    notices: list[tuple[str, str]] = []

    async def call_tool(name, args):
        assert (name, args) == ("task_claim", {"id": 5, "agent": "Desktop_agent3"})
        return {"id": 5, "claimed": False, "safe_wait": {"owner": "server_agent1", "until": None}}

    async def refresh_tasks():
        return None

    app.call_tool = call_tool
    app.refresh_tasks = refresh_tasks
    app.notify = lambda message, severity="information": notices.append((message, severity))

    await app._claim_selected_task("Desktop_agent3")

    assert len(notices) == 1
    assert "server_agent1" in notices[0][0] and notices[0][1] == "warning"
