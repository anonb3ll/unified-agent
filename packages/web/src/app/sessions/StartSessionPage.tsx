import { useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useSearchParams } from "react-router";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import type { DispatchRouteRow } from "../../lib/sessions/types";
import { DISPATCH_CONTEXT_OPTIONS, DISPATCH_ROUTES_QUERY_KEY, fetchDispatchRoutes, fetchDispatchStack, type DispatchContextFlag, type DispatchStackPreview } from "../../lib/api/dispatch";
import { fetchNodes, NODES_QUERY_KEY, type NodeRow } from "../../lib/api/nodes";
import { fetchOverview, OVERVIEW_QUERY_KEY, type OverviewTask } from "../../lib/api/overview";
import { MAX_DISPATCH_PROMPT } from "../../lib/dispatch/types";
import {
  Button,
  Checkbox,
  Combobox,
  Field,
  Input,
  LinkButton,
  PageHeader,
  RadioGroup,
  Select,
  Skeleton,
  Textarea,
  UiLink,
  cx,
} from "../../lib/ui";
import { useDraft } from "../lib/drafts";
import { useSignedInIdentity } from "../lib/identity";
import { useHubWrite, type HubWriteState } from "../lib/mutations";
import { agentLabelParam } from "../lib/urlState";
import { describeReadError } from "../lib/queries";
import { sessionQueryKeys, WRITE_PATHS, type DispatchResult, type DispatchStackResult, type NodeStartResult } from "./api";
import { guardrailsFor, type Guardrail } from "./model";
import { requestFingerprint, sameLaunchRequest, useLaunchGuard } from "./launchGuard";
import { ArmedLaunch, LaunchResolution, type Resolution } from "./LaunchResolution";
import { useWhoami, WriteStatus } from "./parts";
import { HandoffDialog } from "./SessionActions";

export type StartForm = {
  agent: string;
  work: "prompt" | "task";
  taskIds: string[];
  prompt: string;
  host: string;
  project: string;
  model: string;
  context: Record<DispatchContextFlag, boolean>;
  backend: string;
  cwd: string;
  hermesProfile: string;
  permissionMode: string;
  deviceModel: string;
};

export const HUB_HOST = "hub";
const CONTEXT_LABEL: Record<DispatchContextFlag, string> = {
  task_notes: "The task's notes",
  recent_validation: "Recent test and review results",
  related_pr: "The related pull request and worktree",
};
const MODES = [
  { value: "supervised", label: "Ask before changes", description: "The agent asks before editing files or running commands; requests appear in your Inbox." },
  { value: "auto-accept-edits", label: "Edit files without asking", description: "The agent edits files on its own and still asks before running commands." },
  { value: "full-access", label: "Full access", description: "The agent does anything its device allows without asking. Use only for trusted work." },
];

function initialForm(task: string | null, agent: string | null): StartForm {
  return {
    agent: agent ?? "",
    work: task ? "task" : "prompt",
    taskIds: task ? [task] : [],
    prompt: "",
    host: HUB_HOST,
    project: "",
    model: "",
    context: { task_notes: true, recent_validation: true, related_pr: false },
    backend: "",
    cwd: "",
    hermesProfile: "",
    permissionMode: "supervised",
    deviceModel: "",
  };
}

/** This form again, with the same presets. */
function startFormHref(task: string | null, agent: string | null): string {
  const qs = new URLSearchParams();
  if (task) qs.set("task", task);
  if (agent) qs.set("agent", agent);
  const s = qs.toString();
  return s ? `/sessions/new?${s}` : "/sessions/new";
}

function taskLabel(task: OverviewTask): string {
  return `#${task.id} ${task.title || "Untitled task"}`;
}

/** Which launch the form describes, from its fields. */
export function launchKind(form: StartForm): "stack" | "dispatch" | "device" {
  if (form.host !== HUB_HOST) return "device";
  return form.agent ? "dispatch" : "stack";
}

/**
 * `/sessions/new` (spec §9.4 Start a session; legacy Dispatch Studio and the
 * Sessions launcher). One form for three hub routes: the hub picks the next
 * ready task (`dispatch-stack`), a chosen agent on the hub machine
 * (`dispatch`), or a chosen device (`node-session`).
 */
export function StartSessionPage() {
  const [params] = useSearchParams();
  const taskParam = params.get("task");
  const presetTask = taskParam && /^\d+$/.test(taskParam) ? taskParam : null;
  // `?agent=`: opened for one agent (its page's "Start a session"); the draft is kept per preset.
  const presetAgent = agentLabelParam(params.get("agent"));
  const whoami = useWhoami();
  const identity = whoami.data?.agent ?? null;
  const draftIdentity = useSignedInIdentity();
  const draft = useDraft<StartForm>({
    identity: draftIdentity,
    operation: "session.start",
    object: { task: presetTask, agent: presetAgent },
    initial: initialForm(presetTask, presetAgent),
  });
  const form = draft.value;
  // The latest form, so a launch that resolves later compares against what is on screen now.
  const formRef = useRef(form);
  formRef.current = form;
  const set = <K extends keyof StartForm>(key: K, value: StartForm[K]) => draft.setValue((prev) => ({ ...prev, [key]: value }));

  const overview = useQuery({ queryKey: OVERVIEW_QUERY_KEY, queryFn: ({ signal }) => fetchOverview(signal) });
  const routes = useQuery({ queryKey: DISPATCH_ROUTES_QUERY_KEY, queryFn: ({ signal }) => fetchDispatchRoutes(signal) });
  const nodes = useQuery({ queryKey: NODES_QUERY_KEY, queryFn: ({ signal }) => fetchNodes(signal) });
  const [handoff, setHandoff] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const kind = launchKind(form);
  const node: NodeRow | null = kind === "device" ? (nodes.data?.nodes.find((n) => n.node === form.host) ?? null) : null;
  const agentLabel = kind === "device" ? (node?.agent ?? "") : form.agent;
  const taskIds = form.taskIds.map(Number).filter((n) => Number.isSafeInteger(n) && n > 0);

  const tasks = useMemo(() => {
    const health = overview.data?.queue_health;
    const seen = new Set<number>();
    const rows: OverviewTask[] = [];
    for (const t of [
      ...(health?.desktop_open ?? []),
      ...(health?.dispatch_ready ?? []),
      ...(health?.interactive_only ?? []),
      ...(health?.operator_only ?? []),
      ...(overview.data?.tasks?.active ?? []),
    ]) {
      if (!t?.id || seen.has(t.id)) continue;
      seen.add(t.id);
      rows.push(t);
    }
    return rows;
  }, [overview.data]);
  const projects = overview.data?.projects ?? [];
  const agents = overview.data?.agents.agents ?? [];
  const route = routes.data?.routes.find((r) => r.agent_label === form.agent) ?? null;

  const stackPreview = useQuery({
    queryKey: sessionQueryKeys.stackPreview(taskIds, form.project),
    queryFn: ({ signal }) => fetchDispatchStack(taskIds, form.project, signal),
    enabled: kind === "stack" && taskIds.length > 0 && form.project !== "",
  });

  // One launch at a time, shared by every launch in the area: see launchGuard.ts.
  const guard = useLaunchGuard(whoami.data);
  const [resolution, setResolution] = useState<Resolution | null>(null);
  const keyRef = useRef("");
  // The attempt id: hub task #2501 makes these routes honour it.
  const idempotencyKey = () => keyRef.current;

  const dispatch = useHubWrite<Record<string, unknown>, DispatchResult>({
    path: WRITE_PATHS.dispatch,
    // Sent for hub task #2498; the hub ignores it today, so nothing relies on it.
    idempotencyKey,
    label: "Starting the session",
    invalidates: [sessionQueryKeys.all, OVERVIEW_QUERY_KEY],
    // Never replayed. dispatch_prompt() creates a delegation, then calls
    // task_update, then starts work; when a later gate parks, the delegation
    // already exists and a repeat would create a second one. Until the hub
    // offers an idempotent continuation (task #2498) the person submits again
    // from the kept form, after checking what already exists.
    resumable: false,
    successMessage: (data) => (data?.spawned === false ? "Task created; the agent did not start." : "Session started."),
  });
  const stack = useHubWrite<{ task_ids: number[]; project: string }, DispatchStackResult>({
    path: WRITE_PATHS.dispatchStack,
    idempotencyKey,
    label: "Starting the next ready task",
    invalidates: [sessionQueryKeys.all, OVERVIEW_QUERY_KEY],
    successMessage: (data) => (data?.dispatch?.launched === false ? "The task did not start." : "Next ready task started."),
    // Which task launches depends on the queue at the time; a later repeat
    // is not proven to be the approved call.
    resumable: false,
  });
  const device = useHubWrite<Record<string, unknown>, NodeStartResult>({
    path: WRITE_PATHS.nodeSession,
    idempotencyKey,
    label: "Starting the session on the device",
    invalidates: [sessionQueryKeys.all, NODES_QUERY_KEY],
    // Not replayed: node-session start does not deduplicate (task #2498).
    resumable: false,
    successMessage: "Queued on the device.",
  });
  const write = kind === "device" ? device : kind === "dispatch" ? dispatch : stack;
  // The form resets after a successful launch, so the result is shown for the
  // launch that was made, not for whatever the reset form now describes.
  const [last, setLast] = useState<{ kind: "stack" | "dispatch" | "device"; agent: string } | null>(null);
  const lastWrite = last ? (last.kind === "device" ? device : last.kind === "dispatch" ? dispatch : stack) : write;

  // ---- validation, in plain words
  const problems: Partial<Record<"agent" | "work" | "prompt" | "project" | "host" | "cwd" | "backend", string>> = {};
  const prompt = form.prompt.trim();
  if (kind === "stack") {
    if (form.work !== "task" || taskIds.length === 0)
      problems.work = "To let the hub choose, pick one or more tasks. The hub starts the first one that is ready, with the agent it is assigned to.";
    if (!form.project) problems.project = "Pick the project the tasks belong to.";
  }
  // "A task" needs a task on every launch path (review 15), not only when the hub chooses.
  if (kind !== "stack" && form.work === "task" && taskIds.length === 0)
    problems.work = 'Pick the task to work on, or choose "Instructions I write".';
  if (kind === "dispatch") {
    if (form.agent === identity) problems.agent = "Pick an agent other than the one you are signed in as.";
    if (!prompt) problems.prompt = "Say what the agent should do.";
    if (form.prompt.length > MAX_DISPATCH_PROMPT) problems.prompt = `Keep the instructions under ${MAX_DISPATCH_PROMPT} characters.`;
    if (!form.project) problems.project = "Pick the project to work in.";
  }
  if (kind === "device") {
    if (!node) problems.host = "This device is no longer connected. Pick another.";
    if (!prompt) problems.prompt = "Say what the agent should do.";
    if (node && !(form.backend || node.backends[0])) problems.backend = "This device reports no agent program to run.";
    if (node && !(form.cwd || node.worktree_roots[0])) problems.cwd = "Enter the folder to work in.";
    const backendNow = node ? (node.backends.includes(form.backend) ? form.backend : (node.backends[0] ?? "")) : "";
    if (node && backendNow === "acp:hermes" && !effectiveHermesProfile(node, form))
      problems.backend = "No Hermes profile on this device can be started: none is mapped to a known agent.";
  }
  const valid = Object.keys(problems).length === 0;

  // The request Start sends, built once: its fingerprint binds the attempt
  // id to it (task #2510), so an id kept for a retry is only sent with it.
  const request = ((): { kind: "stack"; body: { task_ids: number[]; project: string } } | { kind: "dispatch" | "device"; body: Record<string, unknown> } | null => {
    if (kind === "stack") return { kind, body: { task_ids: taskIds, project: form.project } };
    if (kind === "dispatch") {
      const context = (Object.entries(form.context) as [DispatchContextFlag, boolean][]).filter(([, on]) => on).map(([f]) => f);
      return {
        kind,
        body: {
          agent: form.agent,
          project: form.project,
          prompt,
          task_id: form.work === "task" && taskIds[0] ? taskIds[0] : null,
          context,
          ...(form.model.trim() ? { model: form.model.trim() } : {}),
        },
      };
    }
    if (!node) return null;
    const backend = node.backends.includes(form.backend) ? form.backend : (node.backends[0] ?? "");
    const cwd = form.cwd || node.worktree_roots[0] || "";
    return {
      kind,
      body: {
        action: "start",
        node: node.node,
        backend,
        cwd,
        prompt,
        ...(form.work === "task" && taskIds[0] ? { task_id: taskIds[0] } : {}),
        ...(backend === "acp:hermes" ? { hermes_profile: effectiveHermesProfile(node, form) ?? "" } : {}),
        ...(backend.startsWith("acp:") && devicePermissions(node, form).mode !== "supervised" ? { permission_mode: devicePermissions(node, form).mode } : {}),
        ...(backend.startsWith("acp:") && form.deviceModel ? { model: form.deviceModel } : {}),
      },
    };
  })();
  const target = kind === "dispatch" ? form.agent : kind === "device" ? form.host : form.project;
  const fingerprint = request ? requestFingerprint(kind, { target, taskIds, body: request.body }) : null;
  // A launch the person chose to send again goes out only with the same request.
  const armedMatches = guard.status.kind === "armed" && fingerprint !== null && sameLaunchRequest(guard.status.record, kind, fingerprint);
  const blocked = guard.status.kind !== "free" && !armedMatches;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (!valid) {
      focusFirstProblem(problems, form);
      return;
    }
    if (write.isRunning || blocked || whoami.isPending) return;
    if (!request || !fingerprint) return;
    const key = guard.begin({
      op: kind,
      target,
      taskIds,
      label:
        kind === "stack"
          ? `Next ready task of ${taskIds.map((t) => `#${t}`).join(", ")}`
          : kind === "device"
            ? `${agentLabel || "Device agent"} on ${form.host}${taskIds[0] ? `, task #${taskIds[0]}` : ""}`
            : `${form.agent}${taskIds[0] ? `, task #${taskIds[0]}` : ""}`,
      fingerprint,
    });
    // Stored before it is sent; refused while another launch is unsettled,
    // and a kept attempt id is only reused for the same request.
    if (!key) return;
    setLast({ kind, agent: agentLabel });
    setResolution(null);
    keyRef.current = key;
    const sentRevision = JSON.stringify(form);
    const outcome =
      request.kind === "stack" ? await stack.run(request.body) : request.kind === "dispatch" ? await dispatch.run(request.body) : await device.run(request.body);
    guard.settle(key, outcome);
    if (outcome.kind === "done" || outcome.kind === "accepted") {
      {
        // Clear only the revision that was launched (review 13): edits made
        // while it was sending are kept for the next launch.
        if (JSON.stringify(formRef.current) === sentRevision) draft.clear();
        setSubmitted(false);
      }
    }
  };

  /** The person's explicit choice: the next Start gets a new attempt id. */
  const startNew = () => {
    guard.clear();
    setResolution({ kind: "not_started", reason: "You set the last launch aside: the next Start is a new launch." });
  };

  const loading = overview.isPending || nodes.isPending;

  return (
    <>
      <PageHeader
        title="Start a session"
        description="Give an agent a task or instructions. It works in its own copy of the code and reports back here."
        actions={
          <>
            <Button variant="ghost" onClick={() => setHandoff(true)}>
              Hand off work
            </Button>
            <LinkButton href="/sessions">All sessions</LinkButton>
          </>
        }
      />
      {draft.restored ? <p className="m-0 mb-3 text-sm text-[var(--ua-muted)]">Your unfinished form was restored.</p> : null}
      {overview.isError ? (
        <ReadProblem what="the agents, projects and tasks" error={overview.error} retry={() => void overview.refetch()} />
      ) : null}
      {loading ? (
        <Skeleton label="Loading agents, tasks and devices" lines={6} />
      ) : (
        <form onSubmit={(e) => void submit(e)} noValidate className="grid max-w-3xl gap-5" aria-describedby="start-guardrails">
          <Field
            id="start-host"
            label="Where should it run?"
            hint={
              nodes.isError
                ? "Devices could not be loaded, so only the hub machine is offered."
                : "The hub machine runs agents installed next to the hub. A device runs the agent connected from that computer."
            }
            error={submitted ? problems.host : undefined}
          >
            <Select
              value={form.host}
              onChange={(e) => draft.setValue((prev) => ({ ...prev, host: e.target.value, backend: "", cwd: "", hermesProfile: "", permissionMode: "supervised", deviceModel: "" }))}
              options={[
                { value: HUB_HOST, label: "The hub machine" },
                ...(nodes.data?.nodes ?? []).map((n) => ({ value: n.node, label: `${n.node}${n.agent ? ` (${n.agent})` : ""}` })),
              ]}
            />
          </Field>

          {kind === "device" ? (
            <p className="m-0 text-sm">
              Runs as <span className="font-medium">{agentLabel || "the device's agent"}</span>, the agent connected from this device.
            </p>
          ) : (
            <Field
              id="start-agent"
              label="Agent"
              hint="Let the hub choose to start the next ready task with the agent it is assigned to."
              error={submitted ? problems.agent : undefined}
            >
              <Combobox
                value={form.agent || "__hub__"}
                onChange={(v) => set("agent", !v || v === "__hub__" ? "" : v)}
                options={[
                  { value: "__hub__", label: "Let the hub choose" },
                  ...agents.map((a) => ({
                    value: a.label,
                    label: a.label,
                    description: a.label === identity ? "You are signed in as this agent" : a.status,
                    disabled: a.label === identity,
                  })),
                ]}
              />
            </Field>
          )}

          <RadioGroup
            name="start-work"
            legend="What should it work on?"
            orientation="horizontal"
            value={form.work}
            onChange={(v) => set("work", v === "task" ? "task" : "prompt")}
            options={[
              { value: "prompt", label: "Instructions I write" },
              { value: "task", label: "A task" },
            ]}
          />
          {form.work === "task" ? (
            <TaskPicker
              tasks={tasks}
              selected={form.taskIds}
              multiple={kind === "stack"}
              onChange={(ids) => set("taskIds", ids)}
              error={submitted ? problems.work : undefined}
            />
          ) : kind === "stack" && submitted && problems.work ? (
            <p role="alert" className="m-0 text-sm text-[var(--ua-danger)]">
              {problems.work}
            </p>
          ) : null}

          {kind !== "stack" ? (
            <Field
              id="start-prompt"
              label="Instructions"
              required
              hint={kind === "dispatch" ? `${form.prompt.length} of ${MAX_DISPATCH_PROMPT} characters` : "What should the agent do?"}
              error={submitted ? problems.prompt : undefined}
            >
              <Textarea
                rows={5}
                value={form.prompt}
                maxLength={kind === "dispatch" ? MAX_DISPATCH_PROMPT : 20_000}
                onChange={(e) => set("prompt", e.target.value)}
                placeholder={form.work === "task" ? "What to do on this task, in a sentence or two" : "Describe the outcome you want"}
              />
            </Field>
          ) : null}

          {kind !== "device" ? (
            <Field id="start-project" label="Project" required error={submitted ? problems.project : undefined}>
              <Select
                value={form.project}
                placeholder="Pick a project"
                onChange={(e) => set("project", e.target.value)}
                options={projects.map((p) => ({ value: p.project, label: p.label }))}
              />
            </Field>
          ) : null}

          {kind === "device" && node ? <DeviceOptions node={node} form={form} set={set} problems={submitted ? problems : {}} /> : null}

          {kind === "dispatch" ? (
            <details className="rounded-md border border-[var(--ua-border)] p-3">
              <summary className="cursor-pointer text-sm font-medium">More options</summary>
              <div className="mt-3 grid gap-3">
                <Field label="Model" hint={`Leave empty for the agent's default${defaultModel(routes.data?.model_defaults, form.agent)}.`}>
                  <Input list="start-model-options" value={form.model} onChange={(e) => set("model", e.target.value)} autoComplete="off" />
                </Field>
                <datalist id="start-model-options">
                  {(routes.data?.model_options ?? []).map((m) => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
                <fieldset className="m-0 grid gap-2 border-0 p-0">
                  <legend className="mb-1 text-sm font-medium">Give the agent</legend>
                  {DISPATCH_CONTEXT_OPTIONS.map((flag) => (
                    <Checkbox
                      key={flag}
                      label={CONTEXT_LABEL[flag]}
                      checked={form.context[flag]}
                      onChange={(e) => set("context", { ...form.context, [flag]: e.target.checked })}
                    />
                  ))}
                </fieldset>
              </div>
            </details>
          ) : null}

          <Preview kind={kind} route={route} routesState={routes.isPending ? "loading" : routes.isError ? "error" : "ready"} agent={form.agent} stack={stackPreview} taskIds={taskIds} project={form.project} />

          <GuardrailsLine
            items={
              kind === "device"
                ? deviceGuardrails(node, form, whoami.data ? { enabled: whoami.data.control_enabled !== false, reason: whoami.data.control_disabled_reason ?? null } : null, nodes.data?.acceptance_required === true)
                : guardrailsFor({
                    control: whoami.data ? { enabled: whoami.data.control_enabled !== false, reason: whoami.data.control_disabled_reason ?? null } : null,
                    route,
                    routesState: routes.isPending ? "loading" : routes.isError ? "error" : "ready",
                    agent: form.agent,
                    hubChooses: kind === "stack",
                  })
            }
          />

          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" variant="primary" loading={write.isRunning} disabled={blocked}>
              {kind === "stack" ? "Start the next ready task" : "Start session"}
            </Button>
            {submitted && !valid ? (
              <p role="alert" className="m-0 text-sm text-[var(--ua-danger)]">
                Fix the fields marked above, then start again.
              </p>
            ) : null}
          </div>
          {guard.status.kind === "sending" && !guard.status.here ? (
            <p role="status" className="m-0 text-sm">
              A launch ({guard.status.record.label}) is being sent from another tab. This form waits until it is settled.
            </p>
          ) : null}
          {(guard.status.kind === "unresolved" || guard.status.kind === "parked") && guard.record ? (
            <LaunchResolution
              record={guard.record}
              // Reached only by a definite answer (the hub's, or the person's own pick).
              onResolved={(r) => {
                guard.clear();
                setResolution(r);
              }}
              // Keeps the attempt id and its request, stored, until the next send settles (task #2510).
              onRelaunch={(k) => {
                guard.arm(k);
                setResolution(null);
              }}
              onStartNew={startNew}
            />
          ) : null}
          {guard.status.kind === "armed" ? <ArmedLaunch record={guard.status.record} matches={armedMatches} onStartNew={startNew} /> : null}
          {resolution ? <ResolutionNote resolution={resolution} /> : null}
          <LaunchResult kind={last?.kind ?? kind} state={lastWrite.state} agent={last?.agent ?? agentLabel} formHref={startFormHref(presetTask, presetAgent)} />
        </form>
      )}
      {handoff ? <HandoffDialog session={null} identity={draftIdentity} onClose={() => setHandoff(false)} /> : null}
    </>
  );
}

const PROBLEM_ORDER = ["host", "agent", "work", "prompt", "project", "backend", "cwd"] as const;

/**
 * Move focus to the first invalid control (review finding 17), after the
 * errors render, so a keyboard or screen reader user lands on what to fix
 * and hears its error through `aria-describedby`.
 */
function focusFirstProblem(problems: Partial<Record<(typeof PROBLEM_ORDER)[number], string>>, form: StartForm) {
  const first = PROBLEM_ORDER.find((key) => problems[key]);
  if (!first) return;
  const target = (): HTMLElement | null => {
    if (first !== "work") return document.getElementById(`start-${first}`);
    if (form.work !== "task") return document.querySelector<HTMLElement>('input[name="start-work"][value="task"]');
    return document.getElementById("start-task") ?? document.getElementById("start-tasks");
  };
  setTimeout(() => target()?.focus(), 0);
}

function ResolutionNote({ resolution }: { resolution: Resolution }) {
  if (resolution.kind === "not_started") {
    return (
      <p role="status" className="m-0 text-sm">
        {resolution.reason} You can launch again.
      </p>
    );
  }
  return (
    <p role="status" className="m-0 rounded-md border border-[var(--ua-ok)] p-3 text-sm">
      {resolution.by === "hub" ? "The hub says the last launch started" : "You marked the last launch as started"}: {resolution.label}.{" "}
      {resolution.href ? (
        <UiLink href={resolution.href} className="underline">
          Open it
        </UiLink>
      ) : null}
    </p>
  );
}

function defaultModel(defaults: Record<string, string | null> | undefined, agent: string): string {
  if (!defaults) return "";
  const family = (agent.includes("_") ? agent.split("_").pop() : agent)?.toLowerCase() ?? "";
  const key = family === "cursor" ? "UA_MODEL_CURSOR" : family === "codex" ? "UA_MODEL_CODEX" : family === "agy" ? "UA_MODEL_AGY" : family === "claude" ? "UA_MODEL_CLAUDE" : family.startsWith("pi") ? "UA_MODEL_PI" : "UA_MODEL_DEFAULT";
  const value = defaults[key] ?? defaults.UA_MODEL_DEFAULT ?? null;
  return value ? ` (${value})` : "";
}

function ReadProblem({ what, error, retry }: { what: string; error: unknown; retry: () => void }) {
  const { message, detail } = describeReadError(error);
  return (
    <div role="alert" className="mb-4 rounded-md border border-[var(--ua-danger)] p-3 text-sm">
      <p className="m-0">
        Could not load {what}. {message}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <Button size="sm" onClick={retry}>
          Retry
        </Button>
        {detail ? (
          <details className="text-xs text-[var(--ua-muted)]">
            <summary className="cursor-pointer">Technical detail</summary>
            <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{detail}</pre>
          </details>
        ) : null}
      </div>
    </div>
  );
}

function TaskPicker({
  tasks,
  selected,
  multiple,
  onChange,
  error,
}: {
  tasks: OverviewTask[];
  selected: string[];
  multiple: boolean;
  onChange: (ids: string[]) => void;
  error?: string;
}) {
  const options = tasks.map((t) => ({ value: String(t.id), label: taskLabel(t), description: [t.status, t.project].filter(Boolean).join(" · ") }));
  for (const id of selected) if (!options.some((o) => o.value === id)) options.unshift({ value: id, label: `#${id}`, description: "" });
  if (!multiple) {
    return (
      <Field id="start-task" label="Task" required error={error} hint="The agent gets the task as context and reports back on it.">
        <Combobox options={options} value={selected[0] ?? null} onChange={(v) => onChange(v ? [v] : [])} placeholder="Search tasks by number or title" emptyText="No open task matches" />
      </Field>
    );
  }
  return (
    <fieldset className="m-0 grid gap-2 border-0 p-0" aria-describedby="start-tasks-hint">
      <legend className="mb-1 text-sm font-medium">Tasks</legend>
      <p id="start-tasks-hint" className="m-0 text-xs text-[var(--ua-muted)]">
        Pick one or more. The hub starts the first one whose dependencies are done.
      </p>
      {options.length === 0 ? (
        <p className="m-0 text-sm text-[var(--ua-muted)]">There are no open tasks. Create one in Work first.</p>
      ) : (
        <div id="start-tasks" className="grid max-h-56 gap-1 overflow-y-auto rounded-md border border-[var(--ua-border)] p-2" tabIndex={0} role="group" aria-label="Open tasks">
          {options.map((o) => (
            <Checkbox
              key={o.value}
              label={o.label}
              description={o.description || undefined}
              checked={selected.includes(o.value)}
              onChange={(e) => onChange(e.target.checked ? [...selected, o.value] : selected.filter((v) => v !== o.value))}
            />
          ))}
        </div>
      )}
      {error ? (
        <p role="alert" className="m-0 text-xs font-medium text-[var(--ua-danger)]">
          {error}
        </p>
      ) : null}
    </fieldset>
  );
}

/**
 * The one Hermes profile a device launch uses (review finding 11): the chosen
 * one while it is launchable, else the first launchable profile. The same
 * value is shown, used to look up settings, validated and sent.
 */
export function effectiveHermesProfile(node: NodeRow, form: StartForm): string | null {
  const launchable = (node.hermes_profiles ?? []).filter((p) => p.launchable);
  return launchable.find((p) => p.name === form.hermesProfile)?.name ?? launchable[0]?.name ?? null;
}

/**
 * What the hub's negotiated session settings say about permissions on this
 * device (review blocker 3). `unclassified`: the agent program keeps its own
 * native default and the hub cannot classify it (`mode_control:
 * "native_default_unclassified"`, or the control reports `modes` as
 * unclassified). `unreported`: no settings advertised yet. Only supervised is
 * allowed in either case, and neither is described as safe.
 */
export function devicePermissions(node: NodeRow, form: StartForm) {
  const backend = node.backends.includes(form.backend) ? form.backend : (node.backends[0] ?? "");
  const hermes = backend === "acp:hermes" ? effectiveHermesProfile(node, form) : null;
  const settingsKey = backend + (hermes ? `:${hermes}` : "");
  const negotiated = node.session_settings?.[settingsKey];
  const unclassified =
    negotiated?.mode_control === "native_default_unclassified" || node.controls?.[backend]?.capabilities?.modes?.state === "unclassified";
  const state: "unclassified" | "unreported" | "negotiated" = unclassified ? "unclassified" : negotiated ? "negotiated" : "unreported";
  const allowed = new Set<string>(
    state === "negotiated" ? (negotiated?.available_modes ?? []).map((m) => m.permission_mode ?? "").filter(Boolean) : ["supervised"],
  );
  allowed.add("supervised");
  const mode = allowed.has(form.permissionMode) ? form.permissionMode : "supervised";
  const hubName = negotiated?.available_modes?.find((m) => m.permission_mode === mode)?.name ?? null;
  return { backend, acp: backend.startsWith("acp:"), negotiated, state, allowed, mode, hubName, observedAt: negotiated?.observed_at ?? null };
}

function DeviceOptions({
  node,
  form,
  set,
  problems,
}: {
  node: NodeRow;
  form: StartForm;
  set: <K extends keyof StartForm>(key: K, value: StartForm[K]) => void;
  problems: Partial<Record<string, string>>;
}) {
  const perms = devicePermissions(node, form);
  const { backend, acp, negotiated } = perms;
  const profiles = node.hermes_profiles ?? [];
  return (
    <div className="grid gap-3 rounded-md border border-[var(--ua-border)] p-3">
      <Field id="start-backend" label="Agent program" error={problems.backend} hint="Which program on the device runs the agent.">
        <Select value={backend} onChange={(e) => set("backend", e.target.value)} options={node.backends.map((b) => ({ value: b, label: b }))} />
      </Field>
      {backend === "acp:hermes" ? (
        <Field label="Hermes profile" hint="Only profiles mapped to a known agent can be started.">
          <Select
            value={effectiveHermesProfile(node, form) ?? ""}
            placeholder={effectiveHermesProfile(node, form) ? undefined : "No profile can be started"}
            onChange={(e) => set("hermesProfile", e.target.value)}
            options={profiles.map((p) => ({ value: p.name, label: `${p.name} · ${p.agent ?? "not mapped"}${p.launchable ? "" : ` (${p.reason ?? "unavailable"})`}`, disabled: !p.launchable }))}
          />
        </Field>
      ) : null}
      <Field id="start-cwd" label="Folder to work in" required error={problems.cwd}>
        {node.worktree_roots.length ? (
          <Select value={form.cwd || node.worktree_roots[0]} onChange={(e) => set("cwd", e.target.value)} options={node.worktree_roots.map((r) => ({ value: r, label: r }))} />
        ) : (
          <Input value={form.cwd} onChange={(e) => set("cwd", e.target.value)} placeholder="/path/to/checkout" autoComplete="off" />
        )}
      </Field>
      {acp ? (
        <>
          <RadioGroup
            legend="How much may it do without asking?"
            value={perms.mode}
            onChange={(v) => set("permissionMode", v)}
            options={MODES.map((m) => ({
              ...m,
              // Named from the hub's report only (review 2, finding 9): until the
              // device reports a classified mode, supervised is described neutrally.
              label:
                m.value === "supervised" && perms.state === "unclassified"
                  ? "Supervised (the program's own default)"
                  : m.value === "supervised" && perms.state === "unreported"
                    ? "Supervised; this device has not reported its policy yet"
                    : m.label,
              disabled: !perms.allowed.has(m.value),
              description:
                m.value === "supervised" && perms.state === "unclassified"
                  ? "Keeps the agent program's native default and relays its permission requests to your Inbox. What it does without asking is not classified."
                  : m.value === "supervised" && perms.state === "unreported"
                    ? "Permission requests are relayed to your Inbox. What the agent may do without asking is known once the device reports its settings."
                  : perms.allowed.has(m.value)
                    ? m.description
                    : `${m.description} Not offered by this device.`,
            }))}
          />
          <p className="m-0 text-xs text-[var(--ua-muted)]">
            {perms.state === "unclassified"
              ? "This device's native permission policy is not classified, so only a supervised launch is offered."
              : perms.state === "unreported"
                ? "Settings not reported yet. Supervised is required before the first prompt; unsupported settings stop the launch."
                : `Choices from the device's last report${perms.observedAt ? ` (${perms.observedAt})` : ""}; checked again before the first prompt.`}
          </p>
          {negotiated?.available_models?.length ? (
            <Field label="Model">
              <Select
                value={form.deviceModel}
                placeholder="The agent's default"
                onChange={(e) => set("deviceModel", e.target.value)}
                options={negotiated.available_models.map((m) => ({ value: m.id, label: m.name }))}
              />
            </Field>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function deviceGuardrails(
  node: NodeRow | null,
  form: StartForm,
  control: { enabled: boolean; reason: string | null } | null,
  acceptanceRequired: boolean,
): Guardrail[] {
  const out: Guardrail[] = [];
  if (control === null) out.push({ tone: "unknown", text: "Control state unknown: the hub has not said who is signed in yet." });
  else if (!control.enabled) out.push({ tone: "danger", text: `Starting work is turned off on this hub${control.reason ? `: ${control.reason}` : "."}` });
  else out.push({ tone: "ok", text: "Starting work is turned on for the signed-in identity." });
  if (!node) {
    out.push({ tone: "unknown", text: "Pick a connected device to see its checks." });
    return out;
  }
  const acceptance = node.acceptance?.state;
  if (acceptanceRequired && acceptance !== "accepted")
    out.push({ tone: "warn", text: `This device has not passed its acceptance check${acceptance ? ` (${acceptance})` : ""}, so the hub may refuse the start.` });
  else if (acceptance === "accepted") out.push({ tone: "ok", text: "This device passed its acceptance check." });
  const perms = devicePermissions(node, form);
  if (perms.acp && perms.state === "unclassified") {
    out.push({
      tone: "warn",
      text: "Supervised launch keeps the agent program's own default, with permission requests relayed to your Inbox. Its native permission policy is not classified, so the hub cannot say what it may do without asking.",
    });
  } else if (perms.acp && perms.state === "unreported") {
    out.push({
      tone: "warn",
      text: "This device has not reported its permission settings yet. The launch starts supervised and the device checks again before the first prompt; unsupported settings stop the launch.",
    });
  } else if (perms.acp) {
    const mode = MODES.find((m) => m.value === perms.mode) ?? MODES[0];
    out.push({
      tone: perms.mode === "full-access" ? "danger" : perms.mode === "supervised" ? "ok" : "warn",
      text: `${mode.label}: ${mode.description}${perms.hubName ? ` The device reports this mode as "${perms.hubName}".` : ""}`,
    });
  } else {
    out.push({ tone: "warn", text: "This agent program cannot relay permission requests; the hub asks for approval before it starts." });
  }
  return out;
}

const TONE: Record<Guardrail["tone"], { glyph: string; className: string; word: string }> = {
  ok: { glyph: "✓", className: "text-[var(--ua-ok)]", word: "On" },
  warn: { glyph: "!", className: "text-[var(--ua-warn)]", word: "Check" },
  danger: { glyph: "✕", className: "text-[var(--ua-danger)]", word: "Blocked" },
  unknown: { glyph: "?", className: "text-[var(--ua-muted)]", word: "Unknown" },
};

/** The guardrails line (defect 3): each clause from a hub read, never a constant. */
export function GuardrailsLine({ items }: { items: Guardrail[] }) {
  return (
    <section id="start-guardrails" aria-labelledby="guardrails-heading" className="rounded-md border border-[var(--ua-border)] p-3 text-sm">
      <h2 id="guardrails-heading" className="m-0 mb-1 text-sm font-semibold">
        Guardrails
      </h2>
      <ul className="m-0 flex list-none flex-col gap-1 p-0" data-testid="guardrails">
        {items.map((g, i) => (
          <li key={i} className="flex gap-2">
            <span aria-hidden="true" className={cx("w-4 shrink-0 text-center font-semibold", TONE[g.tone].className)}>
              {TONE[g.tone].glyph}
            </span>
            <span>
              <span className="sr-only">{TONE[g.tone].word}: </span>
              {g.text}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Preview({
  kind,
  route,
  routesState,
  agent,
  stack,
  taskIds,
  project,
}: {
  kind: "stack" | "dispatch" | "device";
  route: DispatchRouteRow | null;
  routesState: "loading" | "error" | "ready";
  agent: string;
  stack: UseQueryResult<DispatchStackPreview>;
  taskIds: number[];
  project: string;
}) {
  let body: ReactNode = null;
  if (kind === "stack") {
    if (!taskIds.length || !project) body = <p className="m-0 text-[var(--ua-muted)]">Pick tasks and a project to see which one would start.</p>;
    else if (stack.isPending) body = <Skeleton label="Checking which task is ready" lines={2} />;
    else if (stack.isError) body = <p className="m-0">Could not check the tasks: {describeReadError(stack.error).message}</p>;
    else if (stack.data) {
      const next = stack.data.next_task_id;
      body = (
        <>
          <p className="m-0">{next ? `Task #${next} starts next.` : "None of these tasks is ready to start: each waits on something."}</p>
          <ul className="m-0 mt-1 list-none p-0 text-xs text-[var(--ua-muted)]">
            {stack.data.items.map((item) => (
              <li key={item.task_id}>
                #{item.task_id} {item.title ?? ""}: {item.eligible ? "ready" : (item.reason ?? "waiting")}
              </li>
            ))}
          </ul>
        </>
      );
    }
  } else if (kind === "dispatch") {
    if (!agent) body = null;
    else if (routesState === "loading") body = <Skeleton label="Checking the agent" lines={1} />;
    else if (routesState === "error") body = <p className="m-0">Could not check how {agent} would start. You can still start it; the hub checks again.</p>;
    else if (!route) body = <p className="m-0">{agent} has no way to start on the hub machine.</p>;
    else
      body = (
        <p className="m-0">
          {route.status === "ready" ? "Ready to start" : route.status === "forbidden" ? "Cannot start: no workspace is bound" : "Cannot start: the program is missing"} · runs{" "}
          {route.binary} ({route.backend}){route.aliases.length ? ` · also known as ${route.aliases.join(", ")}` : ""}
        </p>
      );
  }
  if (!body) return null;
  return (
    <section aria-labelledby="preview-heading" className="rounded-md border border-[var(--ua-border)] bg-[var(--ua-surface)] p-3 text-sm">
      <h2 id="preview-heading" className="m-0 mb-1 text-sm font-semibold">
        Preview
      </h2>
      {body}
    </section>
  );
}

/**
 * The outcome of a launch, said in place. Accepted work (202 without
 * approval markers) is a started session with a link to it; a parked launch
 * says it waits for approval and runs on its own once approved (defect 9).
 */
function LaunchResult({
  kind,
  state,
  agent,
  formHref,
}: {
  kind: "stack" | "dispatch" | "device";
  state: HubWriteState<unknown>;
  agent: string;
  formHref: string;
}) {
  if (state.status === "done" || state.status === "accepted") {
    const data = (state.data ?? {}) as DispatchResult & DispatchStackResult & NodeStartResult;
    if (kind === "stack" && data.dispatch?.launched === false) {
      return (
        <div role="alert" className="rounded-md border border-[var(--ua-danger)] p-3 text-sm">
          The task did not start: {data.dispatch.error || data.dispatch.reason || "the hub did not say why"}. Check the task, then try again.
        </div>
      );
    }
    if (kind === "dispatch" && data.spawned === false) {
      return (
        <div role="alert" className="rounded-md border border-[var(--ua-warn)] p-3 text-sm">
          Task #{data.task_id} was created for {agent}, but the agent did not start: {data.error || data.reason || "the hub did not say why"}.{" "}
          {data.task_id ? (
            <UiLink href={`/work/tasks/${data.task_id}`} className="underline">
              Open the task
            </UiLink>
          ) : null}
        </div>
      );
    }
    const taskId = kind === "stack" ? data.next_task_id : data.task_id;
    const href =
      kind === "device" && data.item_id
        ? `/sessions?tab=work&item=${encodeURIComponent(data.item_id)}`
        : taskId
          ? `/sessions?q=${encodeURIComponent(`#${taskId}`)}`
          : "/sessions?status=open";
    return (
      <div role="status" className="rounded-md border border-[var(--ua-ok)] p-3 text-sm">
        <p className="m-0 font-medium">{kind === "device" ? "The session is queued on the device." : "The session has started."}</p>
        <p className="m-0 mt-1">
          {kind === "device" ? "It opens as soon as the device picks it up." : `${agent || "The agent"} is working${taskId ? ` on task #${taskId}` : ""}.`}{" "}
          <UiLink href={href} className="underline">
            Open the session
          </UiLink>
        </p>
      </div>
    );
  }
  // A parked launch is shown once, by the launch guard's panel (with its
  // approval and the "launch it again" choice), not here as well.
  if (state.status === "pending_approval") return null;
  return <WriteStatus state={state} label="Starting the session" formHref={formHref} />;
}
