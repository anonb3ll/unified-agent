"""Interactive TUI Dashboard for Unified Agent Coordination (Phase 4.3)."""

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    TabbedContent,
    TabPane,
    TextArea,
)

from . import config, post_init, storage, task_ownership

# Conditional imports for remote MCP mode
try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    HAS_MCP = True
except ImportError:
    HAS_MCP = False


_ASSIGNEE_RE = re.compile(r"claimed-by:\s*([A-Za-z0-9][\w.-]*)", re.IGNORECASE)


def _parse_assignee(notes: str) -> str:
    """Extract the 'claimed-by: <agent>' assignee from task notes, or '-' if none."""
    match = _ASSIGNEE_RE.search(notes or "")
    return match.group(1) if match else "-"


def _local_tool_registry() -> dict:
    """Name -> callable map for local (in-process) tool dispatch.

    Derived from ``server.REGISTERED_TOOLS`` with the same profile filter MCP
    ``tools/list`` applies (``profiles.allowed_tools``), so the TUI cannot drift
    from the authorized surface. Playground Select options and keybindings are
    decoration over this set — they do not define it.
    """
    from . import profiles as operational_profiles
    from . import server

    allowed = operational_profiles.allowed_tools(set(server.REGISTERED_TOOLS))
    return {name: server.REGISTERED_TOOLS[name] for name in allowed}


def detect_remote_url() -> str | None:
    """Detect if unified-agent is configured in remote HTTP mode."""
    if env_url := os.environ.get("UA_TUI_REMOTE"):
        return env_url
    if os.environ.get("UA_TRANSPORT") == "http":
        return f"http://{config.http_host()}:{config.http_port()}/mcp"

    try:
        claude_config = Path.home() / ".claude.json"
        if claude_config.exists():
            data = json.loads(claude_config.read_text(encoding="utf-8"))
            mcp_servers = data.get("mcpServers", {})
            ua_config = mcp_servers.get("unified-agent", {})
            if ua_config.get("type") == "http" and ua_config.get("url"):
                return ua_config["url"]
    except Exception:
        pass

    return None


def resolve_tui_bearer_token(agent_label: str | None = None) -> str | None:
    if token := os.environ.get("UA_TUI_BEARER_TOKEN") or os.environ.get("UA_AGENT_BEARER_TOKEN"):
        return token

    try:
        claude_config = Path.home() / ".claude.json"
        if claude_config.exists():
            data = json.loads(claude_config.read_text(encoding="utf-8"))
            mcp_servers = data.get("mcpServers", {})
            ua_config = mcp_servers.get("unified-agent", {})
            headers = ua_config.get("headers", {})
            auth = headers.get("Authorization") or headers.get("authorization")
            if auth and auth.lower().startswith("bearer "):
                return auth[7:].strip()
    except Exception:
        pass

    tokens = config.agent_tokens()
    wanted = [agent_label] if agent_label else []
    # The TUI attributes work to the operator's bare name, but the operator's
    # bearer is minted for their login label.
    if not agent_label or agent_label in config.operator_aliases():
        wanted.append(config.operator_label())
    for label in wanted:
        for tok, lbl in tokens.items():
            if lbl == label:
                return tok
    # Never borrow another identity's token: only an unambiguous single-token
    # install falls back to that token.
    if len(tokens) == 1:
        return next(iter(tokens.keys()))
    return None


class PromptScreen(Screen):
    """Generic popup input screen to prompt user for text."""

    CSS = """
    PromptScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    #prompt-container {
        width: 60;
        height: auto;
        border: solid #a78bfa;
        background: #18181b;
        padding: 1 2;
    }
    #prompt-title {
        text-style: bold;
        color: #a78bfa;
        margin-bottom: 1;
        width: 100%;
        text-align: center;
    }
    #prompt-input {
        margin-bottom: 1;
        border: solid #2d2d30;
    }
    .prompt-buttons {
        layout: horizontal;
        height: auto;
        align-horizontal: right;
    }
    .prompt-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, title: str, default_val: str = ""):
        super().__init__()
        self.title = title
        self.default_val = default_val

    def compose(self) -> ComposeResult:
        yield Container(
            Label(self.title, id="prompt-title"),
            Input(value=self.default_val, id="prompt-input"),
            Horizontal(
                Button("Cancel", id="prompt-cancel", variant="error"),
                Button("Submit", id="prompt-submit", variant="success"),
                classes="prompt-buttons",
            ),
            id="prompt-container",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "prompt-submit":
            val = self.query_one("#prompt-input", Input).value.strip()
            self.dismiss(val)
        else:
            self.dismiss(None)


class SelectScreen(Screen):
    """Popup select list to pick from options."""

    CSS = """
    SelectScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    #select-container {
        width: 40;
        height: auto;
        border: solid #a78bfa;
        background: #18181b;
        padding: 1 2;
    }
    #select-title {
        text-style: bold;
        color: #a78bfa;
        margin-bottom: 1;
        width: 100%;
        text-align: center;
    }
    .select-buttons {
        layout: vertical;
        height: auto;
        align-horizontal: center;
        margin-bottom: 1;
    }
    .select-buttons Button {
        width: 30;
        margin-bottom: 1;
    }
    """

    def __init__(self, title: str, options: list[str]):
        super().__init__()
        self.title = title
        self.options = options

    def compose(self) -> ComposeResult:
        buttons = [Button(opt, id=f"opt-{idx}") for idx, opt in enumerate(self.options)]
        yield Container(
            Label(self.title, id="select-title"),
            Vertical(*buttons, classes="select-buttons"),
            Button("Cancel", id="select-cancel", variant="error"),
            id="select-container",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "select-cancel":
            self.dismiss(None)
        elif event.button.id and event.button.id.startswith("opt-"):
            idx = int(event.button.id.split("-")[1])
            self.dismiss(self.options[idx])


class AddTaskScreen(Screen):
    """Custom modal screen for creating a new task."""

    CSS = """
    AddTaskScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    #add-task-container {
        width: 60;
        height: auto;
        border: solid #a78bfa;
        background: #18181b;
        padding: 1 2;
    }
    #add-title {
        text-style: bold;
        color: #a78bfa;
        margin-bottom: 1;
        width: 100%;
        text-align: center;
    }
    .form-field {
        margin-bottom: 1;
    }
    .form-label {
        text-style: bold;
        color: #e4e4e7;
        margin-bottom: 0;
    }
    .form-buttons {
        layout: horizontal;
        height: auto;
        align-horizontal: right;
        margin-top: 1;
    }
    .form-buttons Button {
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Container(
            Label("Add New Task", id="add-title"),
            Vertical(
                Label("Title *:", classes="form-label"),
                Input(placeholder="Task title", id="task-title-input"),
                classes="form-field",
            ),
            Vertical(
                Label("Priority (high/med/low):", classes="form-label"),
                Input(value="med", id="task-priority-input"),
                classes="form-field",
            ),
            Vertical(
                Label("Notes / Details:", classes="form-label"),
                Input(placeholder="Optional task details", id="task-notes-input"),
                classes="form-field",
            ),
            Horizontal(
                Button("Cancel", id="task-cancel", variant="error"),
                Button("Create", id="task-create", variant="success"),
                classes="form-buttons",
            ),
            id="add-task-container",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "task-create":
            title = self.query_one("#task-title-input", Input).value.strip()
            priority = self.query_one("#task-priority-input", Input).value.strip().lower()
            notes = self.query_one("#task-notes-input", Input).value.strip()

            if not title:
                self.app.bell()
                return
            if priority not in ("high", "med", "low"):
                priority = "med"

            self.dismiss({"title": title, "priority": priority, "notes": notes})
        else:
            self.dismiss(None)


class ViewNoteScreen(Screen):
    """View full memory note detail."""

    CSS = """
    ViewNoteScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    #note-view-container {
        width: 80;
        height: 40;
        border: solid #a78bfa;
        background: #18181b;
        padding: 1 2;
    }
    #note-view-title {
        text-style: bold;
        color: #a78bfa;
        margin-bottom: 1;
        width: 100%;
        text-align: center;
    }
    #note-view-body {
        height: 1fr;
        border: solid #2d2d30;
        background: #09090b;
        padding: 1;
        overflow-y: scroll;
        margin-bottom: 1;
    }
    .note-meta {
        color: #71717a;
        margin-bottom: 1;
    }
    """

    def __init__(self, title: str, meta: str, content: str):
        super().__init__()
        self.note_title = title
        self.meta = meta
        self.content = content

    def compose(self) -> ComposeResult:
        yield Container(
            Label(self.note_title, id="note-view-title"),
            Label(self.meta, classes="note-meta"),
            RichLog(id="note-view-body", wrap=True),
            Button("Close", id="note-view-close", variant="primary"),
            id="note-view-container",
        )

    def on_mount(self) -> None:
        log = self.query_one("#note-view-body", RichLog)
        log.write(self.content)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class EditNotesScreen(Screen):
    """Edit a task's notes in place.

    Notes carry the durable audit trail on a task — gate reasons, PR links, DoD
    evidence — so they change far more often than any other field, yet the
    dashboard could set a task's status and not its notes.
    """

    CSS = """
    EditNotesScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    #notes-edit-container {
        width: 80;
        height: 30;
        border: solid #a78bfa;
        background: #18181b;
        padding: 1 2;
    }
    #notes-edit-title {
        text-style: bold;
        color: #a78bfa;
        margin-bottom: 1;
        width: 100%;
        text-align: center;
    }
    #notes-edit-input {
        height: 1fr;
        margin-bottom: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, task_id: int, current_notes: str):
        super().__init__()
        self.task_id = task_id
        self.current_notes = current_notes

    def compose(self) -> ComposeResult:
        yield Container(
            Label(f"Edit Notes — task #{self.task_id}", id="notes-edit-title"),
            TextArea(self.current_notes, id="notes-edit-input"),
            Horizontal(
                Button("Cancel", id="notes-edit-cancel", variant="error"),
                Button("Save", id="notes-edit-save", variant="success"),
                classes="button-bar",
            ),
            id="notes-edit-container",
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "notes-edit-save":
            # Dismiss with the text even when unchanged; None is reserved to mean
            # "cancelled", so the caller can tell the two apart.
            self.dismiss(self.query_one("#notes-edit-input", TextArea).text)
        else:
            self.dismiss(None)


class DashboardApp(App):
    """Main Textual TUI Application for Dashboard."""

    TITLE = "Unified Agent TUI Control Center"
    SUB_TITLE = "Operations Hub, MCP Playground, and Task Queue"

    CSS = """
    Screen {
        background: #121214;
    }
    Header {
        background: #1e1e24;
        color: #a78bfa;
        text-align: center;
        text-style: bold;
    }
    Footer {
        background: #1e1e24;
        color: #9ca3af;
    }
    TabbedContent {
        background: #121214;
    }
    TabPane {
        padding: 1;
        layout: vertical;
    }
    .pane-title {
        text-style: bold;
        color: #a78bfa;
        margin-bottom: 1;
    }
    
    /* Operations Tab */
    #ops-layout {
        layout: horizontal;
        height: 100%;
    }
    #ops-left {
        width: 50%;
        padding-right: 1;
    }
    #ops-right {
        width: 50%;
        padding-left: 1;
    }
    
    /* Tasks Tab */
    #tasks-layout {
        layout: horizontal;
        height: 100%;
    }
    #left-tasks-pane {
        width: 60%;
        height: 100%;
        border-right: solid #2d2d30;
        padding-right: 1;
    }
    #right-tasks-pane {
        width: 40%;
        height: 100%;
        padding-left: 1;
        layout: vertical;
        background: #18181b;
        border-left: solid #2d2d30;
    }
    .task-detail-label {
        text-style: bold;
        color: #e4e4e7;
        margin-top: 1;
    }
    .task-detail-value {
        color: #a1a1aa;
        margin-bottom: 1;
    }
    #task-notes-area {
        height: 1fr;
        border: solid #2d2d30;
        padding: 1;
        background: #09090b;
        color: #e4e4e7;
        overflow-y: scroll;
        margin-bottom: 1;
    }
    .button-bar {
        layout: horizontal;
        height: auto;
        margin-top: 1;
        align-horizontal: center;
    }
    .button-bar Button {
        margin-right: 1;
    }
    
    /* Memory Search Tab */
    #search-bar {
        layout: horizontal;
        height: auto;
        margin-bottom: 1;
    }
    #search-input {
        width: 1fr;
        border: solid #2d2d30;
    }
    #search-btn {
        width: 15;
        margin-left: 1;
    }
    
    /* MCP Playground */
    #mcp-layout {
        layout: vertical;
        height: 100%;
    }
    #mcp-args-input {
        height: 8;
        border: solid #2d2d30;
        margin-bottom: 1;
    }
    #mcp-response-log {
        height: 1fr;
        border: solid #2d2d30;
        background: #09090b;
    }
    
    /* Live Logs Streamer */
    #logs-stream-log {
        height: 1fr;
        border: solid #2d2d30;
        background: #09090b;
    }
    
    DataTable {
        height: 1fr;
        border: solid #2d2d30;
    }
    RichLog {
        border: solid #2d2d30;
        background: #09090b;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("a", "add_task", "Add Task"),
        ("m", "switch_ops", "Ops Dashboard"),
        ("t", "switch_tasks", "Tasks Queue"),
        ("l", "switch_logs", "Live Logs"),
    ]

    def __init__(self):
        super().__init__()
        self.selected_task_id = None
        self.tasks_data = {}
        self.remote_url = detect_remote_url()
        self.remote_session = None
        self.remote_connected = False
        self.log_streaming = True

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="main-tabs"):
            with TabPane("System Operations", id="ops-tab"):
                with Horizontal(id="ops-layout"):
                    with Vertical(id="ops-left"):
                        yield Label("Systemd User Services", classes="pane-title")
                        yield DataTable(id="services-table")
                        with Horizontal(classes="button-bar"):
                            yield Button("Start Service", id="btn-svc-start", variant="success")
                            yield Button("Stop Service", id="btn-svc-stop", variant="error")
                            yield Button("Restart Service", id="btn-svc-restart", variant="warning")
                    with Vertical(id="ops-right"):
                        yield Label("System Doctor Health Check", classes="pane-title")
                        yield DataTable(id="doctor-table")

            with TabPane("Tasks Queue", id="tasks-tab"):
                with Horizontal(id="tasks-layout"):
                    with Vertical(id="left-tasks-pane"):
                        yield Label("Tasks Queue", classes="pane-title")
                        yield DataTable(id="tasks-table")
                    with Vertical(id="right-tasks-pane"):
                        yield Label("Task Details", classes="pane-title")
                        yield Label("Title:", classes="task-detail-label")
                        yield Label("-", id="detail-title", classes="task-detail-value")
                        yield Label("Priority / Status:", classes="task-detail-label")
                        yield Label("-", id="detail-meta", classes="task-detail-value")
                        yield Label("Assignee (claimed-by):", classes="task-detail-label")
                        yield Label("-", id="detail-assignee", classes="task-detail-value")
                        yield Label("Notes:", classes="task-detail-label")
                        yield RichLog(id="task-notes-area", wrap=True)
                        with Horizontal(classes="button-bar"):
                            yield Button("Claim Task", id="btn-claim", variant="warning")
                            yield Button("Run Review", id="btn-review", variant="primary")
                            yield Button("Set Status", id="btn-status", variant="primary")
                            yield Button("Edit Notes", id="btn-edit-notes", variant="primary")
                            yield Button("Mark Done", id="btn-done", variant="success")
                            yield Button("Delete", id="btn-delete", variant="error")

            with TabPane("Memory & Vault", id="search-tab"):
                yield Label("Search Long-Term Memory Notes", classes="pane-title")
                with Horizontal(id="search-bar"):
                    yield Input(placeholder="Type query to search memory...", id="search-input")
                    yield Button("Search", id="search-btn", variant="primary")
                yield DataTable(id="search-results")

            with TabPane("MCP Playground", id="mcp-tab"):
                yield Label("Execute MCP Tools Interactively", classes="pane-title")
                yield Label("Select Tool:", classes="task-detail-label")
                yield Select(
                    [
                        ("system_doctor", "system_doctor"),
                        ("task_list", "task_list"),
                        ("task_claim", "task_claim"),
                        ("memory_search", "memory_search"),
                        ("review_and_notify", "review_and_notify"),
                        ("vault_journal", "vault_journal"),
                        ("git_worktree_list", "git_worktree_list"),
                    ],
                    id="mcp-tool-select",
                    value="system_doctor",
                )
                yield Label("Tool Arguments (JSON):", classes="task-detail-label")
                yield TextArea("{}", id="mcp-args-input")
                yield Button("EXECUTE TOOL", id="btn-exec-mcp", variant="success")
                yield Label("Response:", classes="task-detail-label")
                yield RichLog(id="mcp-response-log", wrap=True)

            with TabPane("Live Logs Streamer", id="logs-tab"):
                yield Label("Systemd Journal Logs Streamer", classes="pane-title")
                with Horizontal(classes="button-bar"):
                    yield Button("Pause Stream", id="btn-toggle-log", variant="warning")
                    yield Button("Refresh Stream", id="btn-refresh-log", variant="primary")
                yield RichLog(id="logs-stream-log", wrap=True)

            with TabPane("Collaboration Journal", id="journal-tab"):
                yield Label("Obsidian Daily Journal Logs", classes="pane-title")
                yield DataTable(id="journal-table")

            with TabPane("Git Worktrees", id="worktree-tab"):
                yield Label("Active Git Worktrees", classes="pane-title")
                yield DataTable(id="worktree-table")

        yield Footer()

    def on_mount(self) -> None:
        self.setup_tables()
        self.agent_label = os.environ.get("UA_AGENT_LABEL") or config.operator_name()
        post_init.register_default_hooks()
        storage.init_db()
        self.run_worker(self.action_refresh())
        if self.remote_url and HAS_MCP:
            self.title = "Unified Agent TUI Control Center (Remote)"
            self.sub_title = (
                f"Connecting to Remote Hub: {self.remote_url} (Agent: {self.agent_label})"
            )
            self.run_worker(self.connect_remote())
        else:
            self.title = "Unified Agent TUI Control Center (Local)"
            self.sub_title = f"Database: {config.db_path().name} | Workspace: {config.workspace_root().name} | Identity: {self.agent_label}"

    async def connect_remote(self):
        """Persistent connection worker for remote HTTP MCP mode with Bearer auth."""
        try:
            from contextlib import AsyncExitStack

            import httpx2

            headers = {}
            token = resolve_tui_bearer_token(self.agent_label)
            if token:
                headers["Authorization"] = f"Bearer {token}"

            async with AsyncExitStack() as exit_stack:
                http_client = await exit_stack.enter_async_context(
                    httpx2.AsyncClient(
                        headers=headers,
                        timeout=httpx2.Timeout(5.0),
                        follow_redirects=True,
                    )
                )
                client_gen = streamable_http_client(self.remote_url, http_client=http_client)
                read_stream, write_stream = await exit_stack.enter_async_context(client_gen)
                session = ClientSession(read_stream, write_stream)
                await exit_stack.enter_async_context(session)
                await session.initialize()

                self.remote_session = session
                self.remote_connected = True
                self.sub_title = (
                    f"Connected to Remote Hub: {self.remote_url} (Agent: {self.agent_label})"
                )
                self.notify(f"Connected to remote MCP hub as {self.agent_label}!")
                await self.action_refresh()

                while self.remote_connected:
                    await asyncio.sleep(0.5)
        except Exception as e:
            self.notify(
                f"Remote HTTP hub unavailable ({e}) — falling back to direct local mode.",
                severity="warning",
            )
            self.remote_url = None
            self.remote_connected = False
            self.remote_session = None
            self.title = "Unified Agent TUI Control Center (Local Fallback)"
            self.sub_title = f"Database: {config.db_path().name} | Workspace: {config.workspace_root().name} | Identity: {self.agent_label}"
            await self.action_refresh()

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch tool calls either remotely over MCP HTTP or locally in-process."""
        if self.remote_url and self.remote_session:
            res = await self.remote_session.call_tool(name, args)
            if res.is_error:
                raise Exception(res.content[0].text if res.content else "Tool call failed")
            try:
                return json.loads(res.content[0].text)
            except Exception:
                return {"output": res.content[0].text}
        else:
            fn = _local_tool_registry().get(name)
            if fn is None:
                raise ValueError(f"Unknown local tool: {name}")
            token = config.agent_identity_var.set(
                getattr(self, "agent_label", None) or config.operator_name()
            )
            try:
                return fn(**args)
            finally:
                config.agent_identity_var.reset(token)

    #: Column headers for every DataTable in the dashboard, keyed by selector.
    TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
        "#services-table": ("Unit Service", "Active State", "SubState"),
        "#doctor-table": ("Capability", "Status", "Gate / Detail"),
        "#tasks-table": ("ID", "Title", "Priority", "Status", "Assignee", "Created"),
        "#journal-table": ("Time", "Agent/User", "Tags", "Log Entry"),
        "#worktree-table": ("Branch", "Path", "Head Hash"),
        "#search-results": ("ID", "Title", "Tags", "Similarity", "Snippet"),
    }

    def _pane_widget(self, selector: str, widget_type: type) -> Any | None:
        """Return a widget, or None if its pane isn't mounted yet.

        Panes inside TabbedContent mount lazily, so a refresh worker started in
        on_mount can fire before the table it writes to exists. That race is
        expected and non-fatal — skip the update and let the next refresh fill
        it in. Only NoMatches is treated this way: a WrongType (selector points
        at a different widget) is a real bug and must keep raising.
        """
        try:
            return self.query_one(selector, widget_type)
        except NoMatches:
            return None

    def setup_tables(self) -> None:
        for selector, columns in self.TABLE_COLUMNS.items():
            table = self._pane_widget(selector, DataTable)
            if table is None:
                continue
            table.add_columns(*columns)
            table.cursor_type = "row"

    async def action_refresh(self) -> None:
        await self.refresh_services()
        await self.refresh_doctor()
        await self.refresh_tasks()
        await self.refresh_journal()
        await self.refresh_worktrees()
        await self.refresh_logs()

    async def refresh_services(self) -> None:
        table = self._pane_widget("#services-table", DataTable)
        if table is None:
            return
        table.clear()

        for unit in ("unified-agent.service", "unified-agent-executor.service"):
            try:
                proc = subprocess.run(
                    ["systemctl", "--user", "show", unit, "--property=ActiveState,SubState"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                active = "unknown"
                sub = "unknown"
                for line in proc.stdout.splitlines():
                    if line.startswith("ActiveState="):
                        active = line.split("=", 1)[1]
                    elif line.startswith("SubState="):
                        sub = line.split("=", 1)[1]
                table.add_row(unit, active.upper(), sub, key=unit)
            except Exception as e:
                table.add_row(unit, "ERROR", str(e), key=unit)

    async def refresh_doctor(self) -> None:
        table = self._pane_widget("#doctor-table", DataTable)
        if table is None:
            return
        table.clear()

        try:
            res = await self.call_tool("system_doctor", {})
            checks = res.get("checks", [])
            for c in checks:
                status_str = c["status"]
                gate_detail = f"[{c.get('gate', '')}] {c.get('detail', '')}"
                table.add_row(c["capability"], status_str, gate_detail[:60])
        except Exception as e:
            table.add_row("Error", "FAILED", str(e))

    async def refresh_tasks(self) -> None:
        table = self._pane_widget("#tasks-table", DataTable)
        if table is None:
            return
        table.clear()

        try:
            res = await self.call_tool("task_list", {"status": "all"})
            rows = res.get("tasks", [])
            self.tasks_data = {r["id"]: r for r in rows}
        except Exception as e:
            self.notify(f"Failed to refresh tasks: {e}", severity="error")
            return

        for tid, t in self.tasks_data.items():
            assignee = _parse_assignee(t.get("notes") or "")
            created = t.get("created_at", "")
            if "T" in created:
                created = created.split("T")[0] + " " + created.split("T")[1][:5]

            table.add_row(
                str(tid),
                t["title"],
                t["priority"].upper(),
                t["status"].upper(),
                assignee,
                created,
                key=str(tid),
            )

        if self.selected_task_id and self.selected_task_id in self.tasks_data:
            self.show_task_details(self.selected_task_id)
        else:
            self.clear_task_details()

    async def refresh_journal(self) -> None:
        table = self._pane_widget("#journal-table", DataTable)
        if table is None:
            return
        table.clear()

        try:
            res = await self.call_tool("vault_list", {"path": "Logs"})
            entries = res.get("entries", [])
        except Exception as e:
            table.add_row("-", f"Vault logs unavailable: {e}", "-", "-")
            return

        log_files = sorted(
            [
                e
                for e in entries
                if e["type"] == "file" and re.match(r"^\d{4}-\d{2}-\d{2}\.md$", e["name"])
            ],
            key=lambda x: x["name"],
            reverse=True,
        )

        if not log_files:
            table.add_row("-", "No daily log files found.", "-", "-")
            return

        target_log = f"Logs/{log_files[0]['name']}"
        try:
            read_res = await self.call_tool("vault_read", {"path": target_log})
            content = read_res.get("content", "")
        except Exception as e:
            table.add_row("-", f"Failed to read journal file: {e}", "-", "-")
            return

        pattern = re.compile(r"^-\s+\*\*(\d{2}:\d{2})\*\*\s+\[([^\]]+)\]\s+(.*)$")
        parsed_entries = []
        for line in content.splitlines():
            match = pattern.match(line.strip())
            if match:
                time_str = match.group(1)
                agent = match.group(2)
                text = match.group(3)
                tags = " ".join(re.findall(r"#[a-zA-Z0-9_\-/]+", text))
                clean_text = re.sub(r"#[a-zA-Z0-9_\-/]+", "", text).strip()
                parsed_entries.append((time_str, agent, tags, clean_text))

        for time_str, agent, tags, clean_text in reversed(parsed_entries):
            table.add_row(time_str, agent, tags, clean_text)

        if not parsed_entries:
            table.add_row("-", f"No entries in {log_files[0]['name']}.", "-", "-")

    async def refresh_worktrees(self) -> None:
        table = self._pane_widget("#worktree-table", DataTable)
        if table is None:
            return
        table.clear()

        try:
            res = await self.call_tool("git_worktree_list", {"repo_path": "."})
            worktrees = res.get("worktrees", [])
        except Exception as e:
            table.add_row("Error", f"Failed to retrieve worktrees: {e}", "-")
            return

        for wt in worktrees:
            branch = wt.get("branch", "(detached)")
            path_str = wt.get("path", "")
            head_str = wt.get("head", "")[:8] if "head" in wt else "-"

            home = str(Path.home())
            if path_str.startswith(home):
                path_str = "~" + path_str[len(home) :]

            table.add_row(branch, path_str, head_str)

        if not worktrees:
            table.add_row("No worktrees", "Only base checkout is active", "-")

    async def refresh_logs(self) -> None:
        if not self.log_streaming:
            return
        log_widget = self._pane_widget("#logs-stream-log", RichLog)
        if log_widget is None:
            return
        log_widget.clear()

        journalctl = shutil.which("journalctl")
        if not journalctl:
            log_widget.write("[Systemd journalctl not available on this host]")
            return

        try:
            proc = subprocess.run(
                [
                    journalctl,
                    "--user",
                    "-u",
                    "unified-agent",
                    "-u",
                    "unified-agent-executor",
                    "-n",
                    "30",
                    "--no-pager",
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in proc.stdout.splitlines():
                log_widget.write(line)
        except Exception as e:
            log_widget.write(f"[Log fetch error: {e}]")

    def show_task_details(self, tid: int) -> None:
        t = self.tasks_data.get(tid)
        if not t:
            return

        self.query_one("#detail-title", Label).update(t["title"])
        self.query_one("#detail-meta", Label).update(
            f"Priority: {t['priority'].upper()} | Status: {t['status'].upper()}"
        )

        notes = t.get("notes") or ""
        self.query_one("#detail-assignee", Label).update(_parse_assignee(notes))

        log = self.query_one("#task-notes-area", RichLog)
        log.clear()
        log.write(notes)

    def clear_task_details(self) -> None:
        self.query_one("#detail-title", Label).update("-")
        self.query_one("#detail-meta", Label).update("-")
        self.query_one("#detail-assignee", Label).update("-")
        self.query_one("#task-notes-area", RichLog).clear()
        self.selected_task_id = None

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "tasks-table":
            tid = int(event.row_key.value)
            self.selected_task_id = tid
            self.show_task_details(tid)
        elif event.data_table.id == "search-results":
            nid = event.row_key.value
            await self.show_note_detail(nid)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "search-btn":
            await self.action_search()
        elif btn_id == "btn-claim":
            self.claim_selected_task()
        elif btn_id == "btn-review":
            await self.run_task_review()
        elif btn_id == "btn-done":
            await self.mark_selected_task_done()
        elif btn_id == "btn-status":
            self.change_selected_task_status()
        elif btn_id == "btn-edit-notes":
            self.edit_selected_task_notes()
        elif btn_id == "btn-delete":
            self.delete_selected_task()
        elif btn_id == "btn-exec-mcp":
            await self.execute_mcp_playground()
        elif btn_id == "btn-toggle-log":
            self.log_streaming = not self.log_streaming
            btn = self.query_one("#btn-toggle-log", Button)
            btn.label = "Resume Stream" if not self.log_streaming else "Pause Stream"
        elif btn_id == "btn-refresh-log":
            await self.refresh_logs()
        elif btn_id and btn_id.startswith("btn-svc-"):
            await self.handle_service_control(btn_id.replace("btn-svc-", ""))

    async def handle_service_control(self, action: str) -> None:
        table = self.query_one("#services-table", DataTable)
        if table.cursor_row is not None:
            # Get selected unit from cursor
            row_key = table.get_row_at(table.cursor_row)
            unit = row_key[0] if row_key else "unified-agent.service"
        else:
            unit = "unified-agent.service"

        try:
            subprocess.run(["systemctl", "--user", action, unit], check=True, timeout=5)
            self.notify(f"Executed systemctl --user {action} {unit}")
            await self.refresh_services()
        except Exception as e:
            self.notify(f"Service action failed: {e}", severity="error")

    async def execute_mcp_playground(self) -> None:
        select_w = self.query_one("#mcp-tool-select", Select)
        tool_name = str(select_w.value)
        args_text = self.query_one("#mcp-args-input", TextArea).text

        try:
            args_dict = json.loads(args_text) if args_text.strip() else {}
        except Exception as e:
            self.notify(f"Invalid JSON args: {e}", severity="error")
            return

        res_log = self.query_one("#mcp-response-log", RichLog)
        res_log.clear()
        res_log.write(f"Executing {tool_name}({args_dict})...\n")

        try:
            res = await self.call_tool(tool_name, args_dict)
            res_log.write(json.dumps(res, indent=2))
        except Exception as e:
            res_log.write(f"ERROR: {e}")

    async def run_task_review(self) -> None:
        if not self.selected_task_id:
            self.notify("No task selected", severity="warning")
            return

        self.notify(f"Running review_and_notify on task #{self.selected_task_id}...")
        try:
            res = await self.call_tool("review_and_notify", {"task_id": self.selected_task_id})
            self.notify(f"Review result: {res.get('status', 'complete')}")
            await self.refresh_tasks()
        except Exception as e:
            self.notify(f"Review failed: {e}", severity="error")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            await self.action_search()

    async def action_search(self) -> None:
        query = self.query_one("#search-input", Input).value.strip()
        if not query:
            return

        table = self.query_one("#search-results", DataTable)
        table.clear()

        try:
            search_res = await self.call_tool("memory_search", {"query": query, "limit": 20})
            results = search_res.get("results", [])
        except Exception as e:
            table.add_row("Error", f"Search failed: {e}", "-", "-", "-")
            return

        for r in results:
            nid = r["id"]
            title = r["title"] or "Untitled"
            tags_str = ", ".join(r["tags"])
            sim = f"{r['similarity']:.2f}" if r.get("similarity") is not None else "-"
            snip = r["snippet"].replace("\n", " ")

            table.add_row(nid, title, tags_str, sim, snip, key=nid)

        if not results:
            table.add_row("-", "No matching memory notes found.", "-", "-", "-")

    async def show_note_detail(self, nid: str) -> None:
        try:
            row = await self.call_tool("memory_get", {"id": nid})
            title = row.get("title") or "Untitled"
            tags = ", ".join(row.get("tags", []))
            meta = f"ID: {row.get('id')} | Tags: {tags} | Updated: {row.get('updated_at')}"
            content = row.get("content", "")
            self.push_screen(ViewNoteScreen(title, meta, content))
        except Exception as e:
            self.notify(f"Failed to fetch note detail: {e}", severity="error")

    def action_add_task(self) -> None:
        async def _add_task_async(res):
            try:
                await self.call_tool(
                    "task_add",
                    {"title": res["title"], "notes": res["notes"], "priority": res["priority"]},
                )
                await self.refresh_tasks()
            except Exception as e:
                self.notify(f"Failed to add task: {e}", severity="error")

        def handle_add(res):
            if res:
                self.run_worker(_add_task_async(res))

        self.push_screen(AddTaskScreen(), handle_add)

    def action_switch_ops(self) -> None:
        self.query_one("#main-tabs", TabbedContent).active = "ops-tab"

    def action_switch_tasks(self) -> None:
        self.query_one("#main-tabs", TabbedContent).active = "tasks-tab"

    def action_switch_logs(self) -> None:
        self.query_one("#main-tabs", TabbedContent).active = "logs-tab"

    def claim_selected_task(self) -> None:
        if not self.selected_task_id:
            return

        options = [
            "Desktop_agent3",
            "Laptop_agent3",
            "Desktop_Agent1",
            "Laptop_Agent1",
            "Desktop_Agent5",
            config.operator_name(),
        ]

        def handle_select(agent):
            if agent:
                self.run_worker(self._claim_selected_task(agent))

        self.push_screen(SelectScreen("Select Claimant Identity", options), handle_select)

    async def _claim_selected_task(self, agent: str) -> None:
        try:
            result = await self.call_tool(
                "task_claim", {"id": self.selected_task_id, "agent": agent}
            )
            if task_ownership.is_safe_wait(result):
                wait = task_ownership.describe_wait(result["safe_wait"])
                self.notify(f"Not claimed: {wait}", severity="warning")
            await self.refresh_tasks()
        except Exception as e:
            self.notify(f"Failed to claim task: {e}", severity="error")

    async def mark_selected_task_done(self) -> None:
        if not self.selected_task_id:
            return

        try:
            await self.call_tool("task_update", {"id": self.selected_task_id, "status": "done"})
            await self.refresh_tasks()
        except Exception as e:
            self.notify(f"Failed to complete task: {e}", severity="error")

    def edit_selected_task_notes(self) -> None:
        if not self.selected_task_id:
            return

        task = self.tasks_data.get(self.selected_task_id) or {}
        current = task.get("notes") or ""

        async def _save_notes_async(notes):
            try:
                await self.call_tool("task_update", {"id": self.selected_task_id, "notes": notes})
                await self.refresh_tasks()
            except Exception as e:
                self.notify(f"Failed to update notes: {e}", severity="error")

        def handle_edit(notes):
            # None means cancelled. An empty string is a legitimate edit — the
            # user clearing the notes — so test against None, not falsiness.
            if notes is None or notes == current:
                return
            self.run_worker(_save_notes_async(notes))

        self.push_screen(EditNotesScreen(self.selected_task_id, current), handle_edit)

    def change_selected_task_status(self) -> None:
        if not self.selected_task_id:
            return

        options = ["open", "doing", "blocked", "done"]

        async def _change_status_async(status):
            try:
                await self.call_tool("task_update", {"id": self.selected_task_id, "status": status})
                await self.refresh_tasks()
            except Exception as e:
                self.notify(f"Failed to update status: {e}", severity="error")

        def handle_select(status):
            if status:
                self.run_worker(_change_status_async(status))

        self.push_screen(SelectScreen("Select Task Status", options), handle_select)

    def delete_selected_task(self) -> None:
        if not self.selected_task_id:
            return

        async def _delete_task_async():
            try:
                await self.call_tool("task_delete", {"id": self.selected_task_id})
                self.clear_task_details()
                await self.refresh_tasks()
            except Exception as e:
                self.notify(f"Failed to delete task: {e}", severity="error")

        def handle_confirm(val):
            if val == "yes":
                self.run_worker(_delete_task_async())

        self.push_screen(
            PromptScreen("Are you sure you want to delete? (Type 'yes' to confirm)"), handle_confirm
        )


def main() -> None:
    remote_url = detect_remote_url()
    if not remote_url:
        post_init.register_default_hooks()
        storage.init_db()
    app = DashboardApp()
    app.run()


if __name__ == "__main__":
    main()
