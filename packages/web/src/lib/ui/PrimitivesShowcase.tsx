import { useState, type ReactNode } from "react";
import { Button } from "./Button";
import { Checkbox, RadioGroup, Switch } from "./Choice";
import { CodeBlock } from "./CodeBlock";
import { Combobox } from "./Combobox";
import { DataTable, type SortState } from "./DataTable";
import { DescriptionList } from "./DescriptionList";
import { ConfirmDialog, Dialog, Drawer } from "./Dialog";
import { Field } from "./Field";
import { GraphCanvas } from "./GraphCanvas";
import { IconButton } from "./IconButton";
import { Input, Select, Textarea } from "./Input";
import { LinkButton } from "./LinkButton";
import { Markdown } from "./Markdown";
import { Menu } from "./Menu";
import { PageHeader } from "./PageHeader";
import { EmptyState, ErrorState, Skeleton } from "./States";
import { LinkTabs, Tabs } from "./Tabs";
import { ToastProvider, useToast } from "./Toast";
import { FilterBar } from "./Toolbar";

function Block({ title, testId, children }: { title: string; testId: string; children: ReactNode }) {
  return (
    <section data-testid={testId} className="space-y-3">
      <h3 className="m-0 text-base font-semibold text-[var(--ua-fg)]">{title}</h3>
      {children}
    </section>
  );
}

const AGENTS = [
  { value: "Desktop_Agent1", label: "Desktop_Agent1", description: "Claude Code on the desktop" },
  { value: "Desktop_Agent2", label: "Desktop_Agent2", description: "Codex on the desktop" },
  { value: "cloud_agent8", label: "cloud_agent8", description: "Cloud agent" },
];

type Row = { id: string; title: string; status: string };
const ROWS: Row[] = [
  { id: "41", title: "Write the spec", status: "done" },
  { id: "42", title: "Build the foundation", status: "doing" },
  { id: "43", title: "Ship the work area", status: "open" },
];

function ToastDemo() {
  const toast = useToast();
  return (
    <div className="flex flex-wrap gap-2">
      <Button onClick={() => toast.show({ message: "Task created.", tone: "success" })}>Show success</Button>
      <Button onClick={() => toast.show({ message: "Could not save the note.", tone: "error" })}>Show error</Button>
    </div>
  );
}

/** Control Center v2 primitives (spec §5), for visual and axe checks. */
export function PrimitivesShowcase() {
  const [dialog, setDialog] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [deny, setDeny] = useState(false);
  const [reason, setReason] = useState("");
  const [denied, setDenied] = useState<string | null>(null);
  const [agent, setAgent] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState<SortState | null>({ columnId: "title", direction: "ascending" });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const sorted = [...ROWS].sort((a, b) => {
    if (!sort) return 0;
    const key = sort.columnId as keyof Row;
    return (sort.direction === "ascending" ? 1 : -1) * a[key].localeCompare(b[key]);
  });

  return (
    <ToastProvider>
      <section data-testid="showcase-v2" className="space-y-8">
        <h2 className="text-lg font-semibold text-[var(--ua-fg)]">6. Control Center v2 primitives</h2>

        <Block title="Page header and link tabs" testId="showcase-page-header">
          <PageHeader
            title="Work"
            description="Tasks, goals and plans."
            actions={<LinkButton href="#new" variant="primary">New task</LinkButton>}
            tabs={
              <LinkTabs
                label="Work sections"
                items={[
                  { href: "#tasks", label: "Tasks", current: true, count: 3 },
                  { href: "#goals", label: "Goals", count: "–" },
                ]}
              />
            }
          />
        </Block>

        <Block title="Form fields" testId="showcase-fields">
          <div className="grid max-w-xl gap-4">
            <Field label="Title" hint="One line that says what is done when this is done." required>
              <Input defaultValue="Build the foundation" />
            </Field>
            <Field label="Description" error="Add a sentence on why this matters.">
              <Textarea defaultValue="" />
            </Field>
            <Field label="Priority">
              <Select defaultValue="medium" options={[{ value: "high", label: "High" }, { value: "medium", label: "Medium" }, { value: "low", label: "Low" }]} />
            </Field>
            <Field label="Owner">
              <Combobox options={AGENTS} value={agent} onChange={setAgent} placeholder="Choose an agent" />
            </Field>
            <Checkbox label="Only my tasks" description="Hide tasks owned by others." />
            <Switch label="Live updates" defaultChecked />
            <RadioGroup legend="Where it runs" defaultValue="here" options={[{ value: "here", label: "This machine" }, { value: "other", label: "Another machine" }, { value: "cloud", label: "A cloud host" }]} />
          </div>
        </Block>

        <Block title="Buttons and menus" testId="showcase-actions">
          <div className="flex flex-wrap items-center gap-2">
            <LinkButton href="#home">Go home</LinkButton>
            <IconButton aria-label="Refresh" icon={<span>↻</span>} variant="secondary" />
            <Menu
              label="New"
              variant="primary"
              items={[
                { id: "task", label: "Task", onSelect: () => {} },
                { id: "session", label: "Session", onSelect: () => {} },
                { kind: "separator", id: "s" },
                { id: "agent", label: "Agent", onSelect: () => {} },
              ]}
            />
            <Button onClick={() => setDialog(true)}>Open dialog</Button>
            <Button onClick={() => setDrawer(true)}>Open drawer</Button>
            <Button variant="danger" onClick={() => setConfirm(true)}>Delete note</Button>
            <Button variant="danger" onClick={() => setDeny(true)}>Deny with a reason</Button>
          </div>
          <Dialog open={dialog} onClose={() => setDialog(false)} title="Rename plan" footer={<Button variant="primary" onClick={() => setDialog(false)}>Save</Button>}>
            <Field label="Plan name">
              <Input defaultValue="Foundation" />
            </Field>
          </Dialog>
          <Drawer open={drawer} onClose={() => setDrawer(false)} title="Task 42">
            <DescriptionList items={[{ term: "Status", value: "Doing" }, { term: "Owner", value: "Desktop_Agent1" }]} />
          </Drawer>
          <ConfirmDialog
            open={confirm}
            title="Delete this memory note?"
            objectName="project:alpha / decisions"
            consequence="Agents stop finding it in search. You can restore it from the deleted notes."
            confirmLabel="Delete note"
            variant="danger"
            onConfirm={() => setConfirm(false)}
            onCancel={() => setConfirm(false)}
          />
          <ConfirmDialog
            open={deny}
            title="Deny this approval?"
            objectName="task_delete #42"
            consequence="The agent is told it was denied, with your reason."
            confirmLabel="Deny"
            variant="danger"
            confirmDisabled={reason.trim() === ""}
            onConfirm={() => {
              setDenied(reason);
              setDeny(false);
            }}
            onCancel={() => setDeny(false)}
          >
            <Field label="Reason" required>
              <Input value={reason} onChange={(e) => setReason(e.target.value)} />
            </Field>
          </ConfirmDialog>
          <p role="status" className="m-0 text-sm text-[var(--ua-muted)]">
            {denied === null ? "" : `Denied: ${denied}`}
          </p>
          <ToastDemo />
        </Block>

        <Block title="In-page tabs" testId="showcase-tabs">
          <Tabs
            label="Task detail"
            items={[
              { id: "activity", label: "Activity", panel: <p className="m-0 text-sm">Notes, runs and evidence.</p> },
              { id: "comments", label: "Comments", panel: <p className="m-0 text-sm">No comments yet.</p> },
            ]}
          />
        </Block>

        <Block title="Filter bar and table" testId="showcase-filters">
          <FilterBar
            label="Task filters"
            search={{ label: "Search tasks", value: search, onChange: setSearch }}
            filters={[{ id: "status", label: "Status", value: status, onChange: setStatus, options: [{ value: "open", label: "Open" }, { value: "doing", label: "Doing" }] }]}
            onClearAll={() => {
              setSearch("");
              setStatus("");
            }}
          />
          <DataTable<Row>
            caption="Tasks"
            columns={[
              { id: "id", header: "ID", accessor: "id" },
              { id: "title", header: "Title", accessor: "title", sortable: true, rowLink: true },
              { id: "status", header: "Status", accessor: "status", sortable: true },
            ]}
            data={sorted}
            getRowKey={(r) => r.id}
            getRowHref={(r) => `#task-${r.id}`}
            sort={sort}
            onSortChange={setSort}
            selection={{ selected, onChange: setSelected, rowLabel: (r) => r.title }}
            stickyHeader
          />
        </Block>

        <Block title="Data states" testId="showcase-states">
          <Skeleton label="Loading tasks" />
          <EmptyState headingLevel={4} title="No tasks match these filters" description="Clear a filter to see more." action={<Button>Clear filters</Button>} />
          <ErrorState headingLevel={4} message="The hub could not be reached. Check your connection and try again." detail="tasks HTTP 503" onRetry={() => {}} />
        </Block>

        <Block title="Reading content" testId="showcase-content">
          <DescriptionList columns={2} items={[{ term: "Author", value: "Desktop_Agent1" }, { term: "Project", value: "" }]} />
          <CodeBlock label="Command to run" language="bash" code="uv run unified-agent --help" />
          <Markdown source={"# Decision\n\nWe use **react-router** for URLs. See [the spec](https://example.com).\n\n- one\n- [x] two\n\n<script>alert(1)</script>"} />
        </Block>

        <Block title="Graph" testId="showcase-graph">
          <GraphCanvas
            label="Task dependencies"
            height={260}
            nodes={[
              { id: "41", label: "Write the spec", tone: "ok" },
              { id: "42", label: "Build the foundation", tone: "live" },
              { id: "43", label: "Ship the work area" },
            ]}
            edges={[
              { id: "a", source: "41", target: "42", label: "blocks" },
              { id: "b", source: "42", target: "43", label: "blocks" },
            ]}
            legend={[
              { tone: "ok", label: "Done" },
              { tone: "live", label: "In progress" },
            ]}
          />
        </Block>
      </section>
    </ToastProvider>
  );
}
