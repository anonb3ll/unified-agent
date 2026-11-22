/**
 * `/agents/teams?tab=operating` (spec §9.3 Teams; legacy Operating, task
 * #1757): how each department, sprint and run operates. Settings resolve org
 * default, then office, then sprint, then run, and every resolved value names
 * the layer that set it.
 *
 * - Profiles and department bindings are policy: this page shows them and
 *   prepares a draft of the `operating_profiles` block for a policy revision
 *   (`/agents/org`, Policy revisions); it never writes policy itself.
 * - Sprints: create, change status, profile and overrides, link and unlink
 *   tasks (`POST /api/action/operating-sprint`).
 * - Run settings: resolve a task's next run and set or clear its run layer
 *   (`POST /api/action/operating-run`).
 *
 * Every write may be parked for approval: a parked write changes nothing and
 * is shown as waiting, never as done (spec §6). Nothing here is replayed
 * from storage; the person repeats the change after approval.
 */
import { useEffect, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, CodeBlock, ConfirmDialog, DataTable, Field, Input, Select, StatePill, Textarea, UiLink, type Column } from "../../lib/ui";
import {
  formatValue,
  parseOverrides,
  policyBlockDraft,
  resolvedRows,
  SPRINT_STATUSES,
  type OperatingResolved,
  type OperatingSprint,
  type OperatingView,
  type ResolvedRow,
  type RunAction,
  type SprintAction,
} from "../../lib/operating/api";
import { useDraft } from "../lib/drafts";
import { useEpoch } from "../lib/epoch";
import { useIdentity } from "../lib/identity";
import { useHubWrite } from "../lib/mutations";
import { QueryRegion } from "../lib/queries";
import { useUrlParams } from "../lib/urlState";
import { AGENTS_KEYS, fetchOperating, fetchOperatingResolve, fetchOperatingSprints, WRITE_PATHS } from "./api";
import { Section, settleDraftAt, shownFrom, useLiveDraft, WriteFeedback, type ShownOutcome } from "./shared";

const SPRINT_INVALIDATES = [AGENTS_KEYS.operatingSprints, AGENTS_KEYS.operatingResolve];

function EnforcementPill({ enforced }: { enforced: boolean }) {
  return enforced ? <StatePill kind="ok" label="Enforced" /> : <StatePill kind="unknown" label="Recorded" />;
}

function profileOptions(view: OperatingView, emptyLabel: string) {
  return [{ value: "", label: emptyLabel }, ...Object.keys(view.profiles).map((name) => ({ value: name, label: name }))];
}

/** The canonical text of an overrides object ("" for none). */
function overridesText(value: Record<string, unknown> | null | undefined): string {
  return value && Object.keys(value).length ? JSON.stringify(value) : "";
}

/** A draft's canonical text, or the raw text while it is not valid JSON, so drafts compare by content. */
function normalizeOverrides(text: string): string {
  try {
    return overridesText(parseOverrides(text));
  } catch {
    return text;
  }
}

export function OperatingPanel() {
  const data = useQuery({ queryKey: AGENTS_KEYS.operating, queryFn: ({ signal }) => fetchOperating(signal) });
  return (
    <QueryRegion query={data} loadingLabel="Loading operating profiles" errorTitle="Operating profiles could not be loaded" empty={{ title: "No operating data" }}>
      {(view) => (
        <div className="flex flex-col gap-4">
          <p className="m-0 text-sm text-[var(--ua-muted)]">
            Choose how each department, sprint and run operates. Settings resolve org default, then office, then sprint, then run.
          </p>
          {view.error && (
            <p role="alert" className="m-0 text-sm text-[var(--ua-warn)]">
              Operating profiles are unavailable: {view.error}
            </p>
          )}
          <ProfilesSection view={view} />
          <DepartmentsSection view={view} />
          <PolicyDraftSection key={JSON.stringify(policyBlockDraft(view))} view={view} />
          <SprintsSection view={view} />
          <RunSettingsSection view={view} />
        </div>
      )}
    </QueryRegion>
  );
}

// --- Profiles and departments --------------------------------------------------

function ProfilesSection({ view }: { view: OperatingView }) {
  const enforced = new Set(view.enforced_fields);
  const names = Object.keys(view.profiles);
  return (
    <Section
      title="Profiles"
      id="operating-profiles"
      description="Each profile's settings with what it extends applied. Enforced settings are checked live; recorded ones are frozen on each run for whatever reads them."
    >
      {names.length === 0 ? (
        <p className="m-0 text-sm">No operating profiles are configured, so every agent runs with the defaults. A policy revision that adds an operating_profiles block creates them.</p>
      ) : (
        names.map((name) => {
          const profile = view.profiles[name];
          const fields = (view.fields.length ? view.fields : Object.keys(profile.effective)).filter((f) => f in profile.effective);
          return (
            <div key={name} className="flex flex-col gap-2 border-t border-[var(--ua-border)] pt-3 first:border-t-0 first:pt-0">
              <h3 className="m-0 text-sm font-semibold">
                {name}
                {view.default === name ? " (org default)" : ""}
                {profile.extends ? <span className="font-normal text-[var(--ua-muted)]"> · extends {profile.extends}</span> : null}
              </h3>
              {profile.description && <p className="m-0 text-sm text-[var(--ua-muted)]">{profile.description}</p>}
              <DataTable
                columns={[
                  { id: "field", header: "Setting", cell: (f: string) => <span className="font-medium">{f}</span> },
                  { id: "value", header: "Value", cell: (f: string) => <code className="break-all text-xs">{formatValue(profile.effective[f])}</code> },
                  { id: "effect", header: "Effect", cell: (f: string) => <EnforcementPill enforced={enforced.has(f)} /> },
                ]}
                data={fields}
                getRowKey={(f) => f}
                caption={`${name} settings`}
                captionSrOnly
                scrollLabel={`${name} settings`}
                emptyText="This profile sets nothing."
              />
            </div>
          );
        })
      )}
    </Section>
  );
}

function DepartmentsSection({ view }: { view: OperatingView }) {
  const columns: Column<OperatingView["offices"][number]>[] = [
    { id: "office", header: "Office", cell: (o) => <span className="font-medium">{o.name}</span> },
    { id: "lead", header: "Lead", cell: (o) => o.lead },
    { id: "members", header: "Members", cell: (o) => String(o.members.length) },
    { id: "profile", header: "Profile", cell: (o) => o.profile ?? (view.default ? `${view.default} (default)` : "None") },
    {
      id: "comms",
      header: "Communication",
      cell: (o) => {
        const name = o.profile ?? view.default;
        const comms = name ? (view.profiles[name]?.effective.comms as { mode?: string } | undefined) : undefined;
        return comms?.mode ?? "Not set";
      },
    },
  ];
  return (
    <Section title="Departments" id="operating-departments" description="Which profile each office runs on. An office without its own binding runs on the org default.">
      <DataTable columns={columns} data={view.offices} getRowKey={(o) => o.id} caption="Operating profile per office" scrollLabel="Operating profile per office" emptyText="No offices in the org registry." />
    </Section>
  );
}

/** A draft of the `operating_profiles` policy block for a reviewed policy revision; nothing is written here. */
function PolicyDraftSection({ view }: { view: OperatingView }) {
  const [draft, setDraft] = useState(() => JSON.stringify(policyBlockDraft(view), null, 2));
  const [checked, setChecked] = useState<{ ok: boolean; text: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const edit = view.policy_edit;
  const call = edit ? `${edit.tool}(action="${edit.action}", kind="${edit.kind}", document=<live policy with ${edit.block} replaced by this draft>)` : null;
  const check = () => {
    try {
      const value = JSON.parse(draft) as unknown;
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("The block must be a JSON object.");
      const profiles = (value as { profiles?: unknown }).profiles;
      if (!profiles || typeof profiles !== "object" || !Object.keys(profiles).length) throw new Error("The block must name at least one profile.");
      setChecked({ ok: true, text: "The draft parses. The policy revision checks it fully before anything is written." });
    } catch (exc) {
      setChecked({ ok: false, text: exc instanceof SyntaxError ? "The draft is not valid JSON." : exc instanceof Error ? exc.message : "The draft does not parse." });
    }
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(draft);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <Section
      title="Change profiles or department bindings"
      id="operating-policy"
      description="Profiles and office bindings are policy. They change through a policy revision, which is checked, recorded on the evidence ledger and needs approval where the policy says so. Edit the draft here, then commit it as a policy revision."
    >
      {call && <CodeBlock label="Commit it with" code={call} />}
      <Field label="operating_profiles draft (JSON)" error={checked && !checked.ok ? checked.text : undefined}>
        <Textarea
          value={draft}
          rows={10}
          spellCheck={false}
          className="font-mono"
          onChange={(e) => {
            setDraft(e.target.value);
            setChecked(null);
            setCopied(false);
          }}
        />
      </Field>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" onClick={check}>
          Check draft
        </Button>
        <Button size="sm" onClick={() => void copy()}>
          {copied ? "Copied" : "Copy draft"}
        </Button>
        <UiLink href="/agents/org#policy-revisions" className="text-sm text-[var(--ua-link)] underline">
          Open policy revisions
        </UiLink>
      </div>
      {checked?.ok && (
        <p role="status" className="m-0 text-sm">
          {checked.text}
        </p>
      )}
    </Section>
  );
}

// --- Sprints -------------------------------------------------------------------

function SprintsSection({ view }: { view: OperatingView }) {
  const { values, update } = useUrlParams({ sprint_status: "" });
  const status = (SPRINT_STATUSES as readonly string[]).includes(values.sprint_status) ? values.sprint_status : "";
  const sprints = useQuery({ queryKey: [...AGENTS_KEYS.operatingSprints, status], queryFn: ({ signal }) => fetchOperatingSprints(status, signal) });
  return (
    <Section
      title="Sprints"
      id="operating-sprints"
      description="A sprint groups tasks and sets how their runs operate, above the department and below a single run. A task belongs to at most one sprint."
      actions={
        <Field label="Show" className="flex-row items-center gap-2">
          <Select
            value={status}
            onChange={(e) => update({ sprint_status: e.target.value })}
            options={[{ value: "", label: "All" }, ...SPRINT_STATUSES.map((s) => ({ value: s, label: s }))]}
          />
        </Field>
      }
    >
      <SprintCreateForm view={view} />
      <QueryRegion
        query={sprints}
        loadingLabel="Loading sprints"
        errorTitle="Sprints could not be loaded"
        headingLevel={3}
        isEmpty={(d) => d.sprints.length === 0}
        empty={{ title: status ? `No ${status} sprints` : "No sprints yet", description: "Create one above." }}
      >
        {(d) => (
          <ul aria-label="Sprints" className="m-0 flex list-none flex-col gap-3 p-0">
            {d.sprints.map((sprint) => (
              <SprintRow key={sprint.id} sprint={sprint} view={view} />
            ))}
          </ul>
        )}
      </QueryRegion>
    </Section>
  );
}

type SprintForm = { name: string; profile: string; office: string; overrides: string; starts: string; ends: string };
const SPRINT_INITIAL: SprintForm = { name: "", profile: "", office: "", overrides: "", starts: "", ends: "" };
const SPRINT_FIELDS = ["name", "profile", "office", "overrides", "starts", "ends"] as const;

function SprintCreateForm({ view }: { view: OperatingView }) {
  const epoch = useEpoch();
  const identity = useIdentity();
  const draft = useDraft<SprintForm>({ identity, operation: "operating.sprint.add", initial: SPRINT_INITIAL });
  const live = useLiveDraft(draft);
  const form = draft.value;
  const set = (field: keyof SprintForm) => (value: string) => draft.setValue((prev) => ({ ...prev, [field]: value }));
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const write = useHubWrite<SprintAction, unknown>({
    path: WRITE_PATHS.operatingSprint,
    resumable: false,
    invalidates: SPRINT_INVALIDATES,
    label: "Creating the sprint",
    pendingMessage: false,
    successMessage: "Sprint created.",
  });
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const submitted = { ...form };
    if (!submitted.name.trim()) {
      setError("Give the sprint a name.");
      return;
    }
    let overrides: Record<string, unknown>;
    try {
      overrides = parseOverrides(submitted.overrides);
    } catch (exc) {
      setError(exc instanceof Error ? `${exc.message}.` : "The overrides do not parse.");
      return;
    }
    setError(null);
    setShown({ kind: "none" });
    const body: SprintAction = { op: "add", name: submitted.name.trim() };
    if (submitted.profile) body.profile = submitted.profile;
    if (submitted.office) body.org_unit = submitted.office;
    if (Object.keys(overrides).length) body.overrides = overrides;
    // The hub checks the window (ISO dates, start before end) and refuses a sprint whose settings would not resolve.
    if (submitted.starts) body.starts_at = submitted.starts;
    if (submitted.ends) body.ends_at = submitted.ends;
    const keyAtSubmit = draft.key;
    const outcome = await write.run(body);
    epoch.run(() => {
      setShown(shownFrom(outcome));
      if (outcome.kind === "done") settleDraftAt(epoch, live.current, keyAtSubmit, submitted, SPRINT_FIELDS, (prev) => prev);
    });
  };
  return (
    <form aria-label="Create a sprint" onSubmit={(e) => void submit(e)} className="flex flex-col gap-3 rounded-md border border-[var(--ua-border)] p-3">
      <h3 className="m-0 text-sm font-semibold">Create a sprint</h3>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <Field label="Sprint name" required error={error ?? undefined}>
          <Input value={form.name} maxLength={200} onChange={(e) => set("name")(e.target.value)} />
        </Field>
        <Field label="Profile">
          <Select value={form.profile} onChange={(e) => set("profile")(e.target.value)} options={profileOptions(view, "Inherit")} />
        </Field>
        <Field label="Office" hint="Default: the office of whoever claims each task.">
          <Select value={form.office} onChange={(e) => set("office")(e.target.value)} options={[{ value: "", label: "Claimant's office" }, ...view.offices.map((o) => ({ value: o.id, label: o.name }))]} />
        </Field>
        <Field label="Overrides (JSON)" hint='For example {"comms": {"mode": "direct"}}'>
          <Input value={form.overrides} spellCheck={false} className="font-mono" onChange={(e) => set("overrides")(e.target.value)} />
        </Field>
        <Field label="Starts">
          <Input type="date" value={form.starts} onChange={(e) => set("starts")(e.target.value)} />
        </Field>
        <Field label="Ends">
          <Input type="date" value={form.ends} onChange={(e) => set("ends")(e.target.value)} />
        </Field>
      </div>
      <div>
        <Button type="submit" variant="primary" size="sm" loading={write.isRunning}>
          Create sprint
        </Button>
      </div>
      <WriteFeedback state={write.state} shown={shown} what="Creating the sprint" checkHref="/agents/teams?tab=operating" checkLabel="reload the sprint list" />
    </form>
  );
}

type SprintEdit = { status: string; profile: string; overrides: string };

function storedEdit(sprint: OperatingSprint): SprintEdit {
  return { status: sprint.status, profile: sprint.profile ?? "", overrides: overridesText(sprint.overrides) };
}

function sameEdit(a: SprintEdit, b: SprintEdit): boolean {
  return a.status === b.status && a.profile === b.profile && normalizeOverrides(a.overrides) === normalizeOverrides(b.overrides);
}

/**
 * One sprint. Status, profile and overrides are edited together and saved
 * with one click. The edit starts from the stored values (the baseline): a
 * clean edit follows each refresh, a dirty one is never silently replaced,
 * and saving it over values someone changed on the hub since is refused, so
 * another person's change is not undone.
 */
function SprintRow({ sprint, view }: { sprint: OperatingSprint; view: OperatingView }) {
  const epoch = useEpoch();
  const stored = storedEdit(sprint);
  const [edit, setEdit] = useState<SprintEdit>(stored);
  const [baseline, setBaseline] = useState<SprintEdit>(stored);
  // A confirmed save waiting for the refresh that brings it back: until the
  // stored values move off `from`, the row is not judged stale.
  const [awaiting, setAwaiting] = useState<{ submitted: SprintEdit; from: SprintEdit } | null>(null);
  const [linkTask, setLinkTask] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const [what, setWhat] = useState(`Saving sprint ${sprint.id}`);
  const dirty = !sameEdit(edit, baseline);
  const stale = awaiting === null && dirty && !sameEdit(stored, baseline);
  const storedKey = JSON.stringify(stored);

  useEffect(() => {
    if (awaiting !== null) {
      if (sameEdit(stored, awaiting.from)) return;
      setBaseline(awaiting.submitted);
      setAwaiting(null);
      return;
    }
    if (!sameEdit(stored, baseline) && !dirty) {
      setEdit(stored);
      setBaseline(stored);
    }
    // Keyed on the stored content.
  }, [storedKey, baseline, dirty, awaiting]);

  const write = useHubWrite<SprintAction, unknown>({
    path: WRITE_PATHS.operatingSprint,
    resumable: false,
    invalidates: SPRINT_INVALIDATES,
    label: `Changing sprint ${sprint.id}`,
    pendingMessage: false,
  });
  const [done, setDone] = useState<string | null>(null);

  const send = async (body: SprintAction, label: string, after?: () => void) => {
    setWhat(label);
    setDone(null);
    setShown({ kind: "none" });
    const outcome = await write.run(body);
    epoch.run(() => {
      setShown(shownFrom(outcome));
      if (outcome.kind === "done") {
        setDone(`${label}: done.`);
        after?.();
      }
    });
  };

  const save = () => {
    if (stale) {
      setError("This sprint changed on the hub since you started editing. Reload its values first.");
      return;
    }
    let overrides: Record<string, unknown>;
    try {
      overrides = parseOverrides(edit.overrides);
    } catch (exc) {
      setError(exc instanceof Error ? `${exc.message}.` : "The overrides do not parse.");
      return;
    }
    setError(null);
    const body: SprintAction = { op: "update", sprint_id: sprint.id };
    if (edit.status !== baseline.status) body.status = edit.status;
    if (edit.profile !== baseline.profile) body.profile = edit.profile || null;
    if (normalizeOverrides(edit.overrides) !== normalizeOverrides(baseline.overrides)) body.overrides = overrides;
    const submitted: SprintEdit = { ...edit, overrides: overridesText(overrides) };
    const from = stored;
    void send(body, `Saving sprint ${sprint.id}`, () => {
      setEdit(submitted);
      if (sameEdit(submitted, from)) setBaseline(submitted);
      else setAwaiting({ submitted, from });
    });
  };

  const reload = () => {
    setEdit(stored);
    setBaseline(stored);
    setError(null);
  };

  const link = (event: FormEvent) => {
    event.preventDefault();
    const taskId = Number(linkTask.trim());
    if (!Number.isSafeInteger(taskId) || taskId <= 0) {
      setError("A task id is a positive whole number.");
      return;
    }
    setError(null);
    void send({ op: "link", sprint_id: sprint.id, task_id: taskId }, `Linking task #${taskId} to sprint ${sprint.id}`, () => setLinkTask(""));
  };

  return (
    <li aria-label={`Sprint ${sprint.id}: ${sprint.name}`} className="flex flex-col gap-3 rounded-md border border-[var(--ua-border)] p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">
          #{sprint.id} {sprint.name}
        </span>
        <StatePill kind={sprint.status === "active" ? "live" : sprint.status === "closed" ? "ok" : "unknown"} label={sprint.status} />
        {sprint.org_unit && <span className="text-[var(--ua-muted)]">office {sprint.org_unit}</span>}
        {(sprint.starts_at || sprint.ends_at) && (
          <span className="text-[var(--ua-muted)]">
            {sprint.starts_at ?? "open start"} to {sprint.ends_at ?? "open end"}
          </span>
        )}
        <span className="text-[var(--ua-muted)]">created by {sprint.created_by}</span>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label={`Status of sprint ${sprint.id}`}>
          <Select value={edit.status} onChange={(e) => setEdit({ ...edit, status: e.target.value })} options={SPRINT_STATUSES.map((s) => ({ value: s, label: s }))} />
        </Field>
        <Field label={`Profile of sprint ${sprint.id}`}>
          <Select value={edit.profile} onChange={(e) => setEdit({ ...edit, profile: e.target.value })} options={profileOptions(view, "Inherit")} />
        </Field>
        <Field label={`Overrides of sprint ${sprint.id} (JSON)`}>
          <Input value={edit.overrides} spellCheck={false} className="font-mono" placeholder='{"planning": "P3"}' onChange={(e) => setEdit({ ...edit, overrides: e.target.value })} />
        </Field>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="primary" disabled={!dirty} loading={write.isRunning && what.startsWith("Saving")} onClick={save}>
          Save sprint {sprint.id}
        </Button>
        {dirty && (
          <Button size="sm" variant="ghost" onClick={reload}>
            {stale ? "Reload the hub's values" : "Discard changes"}
          </Button>
        )}
        {stale && <span className="text-sm text-[var(--ua-warn)]">Changed on the hub since you started editing.</span>}
      </div>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-[var(--ua-muted)]">Tasks:</span>
        {sprint.task_ids.length === 0 && <span className="text-[var(--ua-muted)]">none linked</span>}
        {sprint.task_ids.map((taskId) => (
          <span key={taskId} className="inline-flex items-center gap-1 rounded border border-[var(--ua-border)] px-1.5">
            <UiLink href={`/work/tasks/${taskId}`} className="text-[var(--ua-link)] underline">
              #{taskId}
            </UiLink>
            <Button
              size="sm"
              variant="ghost"
              aria-label={`Unlink task ${taskId} from sprint ${sprint.id}`}
              disabled={write.isRunning}
              onClick={() => void send({ op: "unlink", sprint_id: sprint.id, task_id: taskId }, `Unlinking task #${taskId} from sprint ${sprint.id}`)}
            >
              Unlink
            </Button>
          </span>
        ))}
        <form onSubmit={link} className="flex items-end gap-1" aria-label={`Link a task to sprint ${sprint.id}`}>
          <Field label={`Task id to link to sprint ${sprint.id}`} labelHidden>
            <Input value={linkTask} inputMode="numeric" placeholder="task id" className="w-28" onChange={(e) => setLinkTask(e.target.value)} />
          </Field>
          <Button size="sm" type="submit" disabled={write.isRunning}>
            Link task
          </Button>
        </form>
      </div>
      {error && (
        <p role="alert" className="m-0 text-sm text-[var(--ua-danger)]">
          {error}
        </p>
      )}
      {done && write.state.status === "done" && (
        <p role="status" className="m-0 text-sm">
          {done}
        </p>
      )}
      <WriteFeedback state={write.state} shown={shown} what={what} checkHref="/agents/teams?tab=operating" checkLabel="reload the sprint list" />
    </li>
  );
}

// --- Run settings --------------------------------------------------------------

function ResolvedTable({ resolved }: { resolved: OperatingResolved }) {
  const rows = resolvedRows(resolved);
  const columns: Column<ResolvedRow>[] = [
    { id: "field", header: "Setting", cell: (r) => <span className="font-medium">{r.key}</span> },
    { id: "value", header: "Value", cell: (r) => <code className="break-all text-xs">{formatValue(r.value)}</code> },
    { id: "source", header: "Set by", cell: (r) => r.source ?? "Unknown" },
    { id: "effect", header: "Effect", cell: (r) => <EnforcementPill enforced={r.enforcement === "enforced"} /> },
  ];
  return (
    <div className="flex flex-col gap-2">
      <p className="m-0 text-sm text-[var(--ua-muted)]">
        Task #{resolved.task_id}
        {resolved.agent ? ` · claimed by ${resolved.agent}` : ""}
        {resolved.unit ? ` · office ${resolved.unit}` : ""}
        {resolved.sprint_id ? ` · sprint #${resolved.sprint_id}` : ""} · fingerprint <code>{resolved.fingerprint}</code>
      </p>
      <DataTable columns={columns} data={rows} getRowKey={(r) => r.key} caption="Resolved run settings" scrollLabel="Resolved run settings" emptyText="No settings apply to this run." />
    </div>
  );
}

function RunSettingsSection({ view }: { view: OperatingView }) {
  const { values, update } = useUrlParams({ run_task: "" });
  const [input, setInput] = useState(values.run_task);
  const [error, setError] = useState<string | null>(null);
  const parsed = Number(values.run_task);
  const taskId = Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
  const load = (event: FormEvent) => {
    event.preventDefault();
    const next = Number(input.trim());
    if (!Number.isSafeInteger(next) || next <= 0) {
      setError("A task id is a positive whole number.");
      return;
    }
    setError(null);
    update({ run_task: String(next) }, { push: true });
  };
  return (
    <Section title="Run settings" id="operating-run" description="How the next run of one task operates. A run already in progress keeps the settings it was claimed with.">
      <form onSubmit={load} aria-label="Show a task's run settings" className="flex flex-wrap items-end gap-2">
        <Field label="Task id" error={error ?? undefined}>
          <Input value={input} inputMode="numeric" className="w-32" onChange={(e) => setInput(e.target.value)} />
        </Field>
        <Button type="submit" size="sm">
          Show resolved settings
        </Button>
      </form>
      {taskId !== null && <RunSettings key={taskId} taskId={taskId} view={view} />}
    </Section>
  );
}

/** One task's resolved run and its run layer. Keyed by task: nothing carries over between tasks. */
function RunSettings({ taskId, view }: { taskId: number; view: OperatingView }) {
  const epoch = useEpoch();
  const resolved = useQuery({ queryKey: [...AGENTS_KEYS.operatingResolve, taskId], queryFn: ({ signal }) => fetchOperatingResolve(taskId, signal) });
  const [profile, setProfile] = useState("");
  const [overrides, setOverrides] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const [clearing, setClearing] = useState(false);
  const [what, setWhat] = useState(`Setting task #${taskId}'s next run`);
  // `set` replaces the stored run layer, so the form starts from what is
  // stored: an edit to one field keeps the other. Keyed on the stored
  // content, not its timestamp: a changed layer re-seeds the form, an
  // unchanged refetch keeps unsaved edits.
  const stored = resolved.data?.run_settings;
  const storedKey = JSON.stringify(stored ? { profile: stored.profile, overrides: stored.overrides } : null);
  useEffect(() => {
    setProfile(stored?.profile ?? "");
    setOverrides(overridesText(stored?.overrides));
  }, [storedKey]);

  const write = useHubWrite<RunAction, unknown>({
    path: WRITE_PATHS.operatingRun,
    resumable: false,
    invalidates: [AGENTS_KEYS.operatingResolve],
    label: `Changing task #${taskId}'s run settings`,
    pendingMessage: false,
  });
  const [done, setDone] = useState<string | null>(null);
  const send = async (body: RunAction, label: string) => {
    setWhat(label);
    setDone(null);
    setShown({ kind: "none" });
    const outcome = await write.run(body);
    epoch.run(() => {
      setShown(shownFrom(outcome));
      if (outcome.kind === "done") setDone(`${label}: done. The next run of task #${taskId} uses the settings above.`);
      if (outcome.kind !== "error") setClearing(false);
    });
  };
  const setNext = () => {
    let parsedOverrides: Record<string, unknown>;
    try {
      parsedOverrides = parseOverrides(overrides);
    } catch (exc) {
      setError(exc instanceof Error ? `${exc.message}.` : "The overrides do not parse.");
      return;
    }
    if (!profile && !Object.keys(parsedOverrides).length) {
      setError("Pick a profile, give overrides, or both. To remove the run settings, clear them.");
      return;
    }
    setError(null);
    void send(
      { op: "set", task_id: taskId, ...(profile ? { profile } : {}), ...(Object.keys(parsedOverrides).length ? { overrides: parsedOverrides } : {}) },
      `Setting task #${taskId}'s next run`,
    );
  };
  return (
    <div className="flex flex-col gap-3">
      <QueryRegion query={resolved} loadingLabel={`Resolving task #${taskId}`} errorTitle={`Task #${taskId}'s run settings could not be resolved`} headingLevel={3} empty={{ title: "Nothing resolved" }}>
        {(r) => <ResolvedTable resolved={r} />}
      </QueryRegion>
      {stored && (
        <p className="m-0 text-sm">
          Run layer set by {stored.set_by}, {stored.set_at}.
        </p>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Run profile">
          <Select value={profile} onChange={(e) => setProfile(e.target.value)} options={profileOptions(view, "Inherit")} />
        </Field>
        <Field label="Run overrides (JSON)" error={error ?? undefined} hint='For example {"comms": {"mode": "direct"}}'>
          <Input value={overrides} spellCheck={false} className="font-mono" onChange={(e) => setOverrides(e.target.value)} />
        </Field>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="primary" loading={write.isRunning && what.startsWith("Setting")} onClick={setNext}>
          Set for next run
        </Button>
        <Button size="sm" variant="danger" disabled={!stored || write.isRunning} onClick={() => setClearing(true)}>
          Clear run settings
        </Button>
      </div>
      {done && write.state.status === "done" && (
        <p role="status" className="m-0 text-sm">
          {done}
        </p>
      )}
      <WriteFeedback state={write.state} shown={shown} what={what} checkHref={`/agents/teams?tab=operating&run_task=${taskId}`} checkLabel="reload the run settings" />
      {clearing && (
        <ConfirmDialog
          open
          title={`Clear task #${taskId}'s run settings?`}
          objectName={`task #${taskId}`}
          consequence="The next run falls back to its sprint, office and org default settings. A run already in progress keeps its settings."
          confirmLabel="Clear run settings"
          variant="danger"
          busy={write.isRunning}
          onCancel={() => setClearing(false)}
          onConfirm={() => void send({ op: "clear", task_id: taskId }, `Clearing task #${taskId}'s run settings`)}
        />
      )}
    </div>
  );
}
