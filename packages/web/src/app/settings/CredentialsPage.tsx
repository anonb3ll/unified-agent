/**
 * Credentials (spec §9.7): whether each provider has a credential in the
 * pool (present or missing, never a value) and the operator's revoke of a
 * session credential, behind a confirm dialog that names what is revoked
 * and what follows (`POST /api/action/session-credential-revoke`).
 *
 * Nothing on this page renders, stores or logs a secret. The pool read is
 * metadata only (`api._credential_pool_row`), and the revoke form takes the
 * public handles the hub uses (a credential id, a session run id, a machine
 * name), never a token.
 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge, Button, ConfirmDialog, DataTable, Field, Input, RadioGroup, StatePill, UiLink, type Column } from "../../lib/ui";
import { useSignedInIdentity } from "../lib/identity";
import { useEpoch, type OwnerEpoch } from "../lib/epoch";
import type { CredentialPoolRow } from "../../lib/api/quota";
import { backoffRefetchInterval } from "../../lib/polling";
import { useHubWrite } from "../lib/mutations";
import {
  fetchCredentialPool,
  REVOKE_PATH,
  SELECTOR_PATTERNS,
  SETTINGS_KEYS,
  type CredentialPoolPayload,
  type RevokeBody,
  type RevokeResult,
  type RevokeSelector,
} from "./api";
import { cancelledWrite, newKey, Notice, plural, Section, SettingsHeader, SettingsRegion, useLatest, WriteFeedback } from "./shared";

export type ProviderStatus = { provider: string; present: boolean; registered: number; revoked: number; rows: CredentialPoolRow[] };

/** One line per provider: present when any credential is registered, missing when every one was revoked. */
export function providerStatuses(rows: readonly CredentialPoolRow[]): ProviderStatus[] {
  const map = new Map<string, ProviderStatus>();
  for (const row of rows) {
    const provider = row.provider || "Unnamed provider";
    const entry = map.get(provider) ?? { provider, present: false, registered: 0, revoked: 0, rows: [] };
    entry.rows.push(row);
    if (row.status === "revoked") entry.revoked += 1;
    else {
      entry.registered += 1;
      entry.present = true;
    }
    map.set(provider, entry);
  }
  return [...map.values()].sort((a, b) => Number(a.present) - Number(b.present) || a.provider.localeCompare(b.provider));
}

export function CredentialsPage() {
  // The pool records no hub events, so it is polled at a calm interval.
  const pool = useQuery({
    queryKey: SETTINGS_KEYS.credentials,
    queryFn: ({ signal }) => fetchCredentialPool(signal),
    refetchInterval: backoffRefetchInterval(60_000),
  });
  return (
    <>
      <SettingsHeader
        title="Credentials"
        description="Whether each provider has a credential the hub can use, and stopping a session's access to the hub. Credential values are never shown here."
      />
      <div className="flex flex-col gap-4">
        <Section id="credentials-pool" title="Provider credentials" description="The credential pool keeps a label, status and dates for each credential. Registering or revoking a pool credential is done from the command line.">
          <SettingsRegion
            query={pool}
            what="the credential pool"
            loadingLabel="Loading provider credentials"
            errorTitle="Provider credentials could not be loaded"
            headingLevel={3}
            isEmpty={(d) => d.credentials.length === 0}
            empty={{ title: "No provider credentials", description: "No credential has been registered in the pool on this hub." }}
          >
            {(data) => <Pool data={data} />}
          </SettingsRegion>
        </Section>
        <RevokeSection />
      </div>
    </>
  );
}

const ROW_COLUMNS: Column<CredentialPoolRow>[] = [
  { id: "label", header: "Label", cell: (r) => <span className="font-medium">{r.label || "Unlabelled"}</span> },
  {
    id: "status",
    header: "Status",
    cell: (r) =>
      r.status === "revoked" ? <StatePill kind="unknown" label="Revoked" /> : <StatePill kind="ok" label={r.active ? "Registered, in use" : "Registered"} />,
  },
  { id: "id", header: "Id", cell: (r) => <code className="font-mono text-xs">{r.id}</code> },
  { id: "updated", header: "Changed", cell: (r) => <span className="text-xs">{r.updated_at || r.created_at || "–"}</span> },
  { id: "note", header: "Note", cell: (r) => <span className="text-xs">{r.note || "–"}</span> },
];

function Pool({ data }: { data: CredentialPoolPayload }) {
  const providers = providerStatuses(data.credentials);
  return (
    <div className="flex flex-col gap-3">
      <p className="m-0 text-sm text-[var(--ua-muted)]">
        {plural(providers.length, "provider")}, {plural(data.credentials.length, "credential")} in the pool.
      </p>
      <ul className="m-0 flex list-none flex-col gap-3 p-0">
        {providers.map((p) => (
          <li key={p.provider} className="rounded-md border border-[var(--ua-border)] p-3" data-provider={p.provider} data-present={p.present}>
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="m-0 min-w-0 flex-1 break-words text-sm font-semibold">{p.provider}</h3>
              {p.present ? <StatePill kind="ok" label="Present" /> : <StatePill kind="danger" label="Missing" />}
              <Badge>{plural(p.registered, "registered", "registered")}</Badge>
              {p.revoked > 0 && <Badge>{plural(p.revoked, "revoked", "revoked")}</Badge>}
            </div>
            {!p.present && <p className="m-0 mt-1 text-sm text-[var(--ua-muted)]">Every credential for this provider was revoked. Register a new one from the command line before work that needs it.</p>}
            <details className="mt-2">
              <summary className="cursor-pointer text-sm">Show {plural(p.rows.length, "credential")}</summary>
              <div className="mt-2">
                <DataTable columns={ROW_COLUMNS} data={p.rows} getRowKey={(r) => r.id} caption={`Credentials for ${p.provider}`} scrollLabel={`Credentials for ${p.provider} (scrolls sideways)`} />
              </div>
            </details>
          </li>
        ))}
      </ul>
    </div>
  );
}

// --- Revoke a session credential ---------------------------------------------

const SELECTORS: { value: RevokeSelector; label: string; description: string; field: string; hint: string; object: (v: string) => string; consequence: string }[] = [
  {
    value: "credential_id",
    label: "One credential",
    description: "The credential with this id, from the session's activity or the hub log.",
    field: "Credential id",
    hint: "16 characters, 0–9 and a–f.",
    object: (v) => `Session credential ${v}`,
    consequence: "The session holding this credential is refused on its next request to the hub and can no longer use hub tools.",
  },
  {
    value: "item_id",
    label: "Every credential of one session run",
    description: "All credentials the hub issued for one queued session run.",
    field: "Session run id",
    hint: "Starts with nd- followed by 16 characters, 0–9 and a–f.",
    object: (v) => `Every credential of session run ${v}`,
    consequence: "That session run is refused on its next request to the hub and can no longer use hub tools.",
  },
  {
    value: "node",
    label: "Every credential on one machine",
    description: "All session credentials issued to sessions on one machine.",
    field: "Machine name",
    hint: "The name the machine registered with, for example desktop-1.",
    object: (v) => `Every session credential on machine ${v}`,
    consequence: "Every session on that machine that uses a session credential is refused on its next request to the hub and can no longer use hub tools.",
  },
];

type RevokeForm = { selector: RevokeSelector; value: string };

export function selectorProblem(selector: RevokeSelector, value: string): string | null {
  const v = value.trim();
  const def = SELECTORS.find((s) => s.value === selector);
  if (!v) return `Enter the ${def?.field.toLowerCase() ?? "value"}.`;
  if (!SELECTOR_PATTERNS[selector].test(v)) return `This is not a ${def?.field.toLowerCase() ?? "value"}. ${def?.hint ?? ""}`.trim();
  return null;
}

/**
 * A revoke whose answer was lost. Only the kind of selector, the time and
 * the identity are kept, never the id or machine name. It lives in the
 * owner epoch's storage under the draft prefix, so sign-out drops it with
 * the drafts.
 */
export const UNRESOLVED_KEY = "draft:settings.revoke-unresolved";
type Unresolved = { identity: string | null; at: number; kind: RevokeSelector };

const KIND_TEXT: Record<RevokeSelector, string> = {
  credential_id: "one session credential",
  item_id: "every credential of one session run",
  node: "every session credential on one machine",
};

function readUnresolved(epoch: OwnerEpoch, identity: string | null | undefined): Unresolved | null {
  if (identity === undefined) return null;
  const value = epoch.storage.read<Unresolved | null>(UNRESOLVED_KEY, null);
  if (!value || typeof value.at !== "number" || !(value.kind in KIND_TEXT)) return null;
  return value.identity === identity ? value : null;
}

const EMPTY_FORM: RevokeForm = { selector: "credential_id", value: "" };

/**
 * The revoke form keeps its input in memory only (no draft, no storage,
 * nothing in the URL): a credential id or a mistakenly pasted token must
 * never reach sessionStorage or localStorage.
 */
function RevokeSection() {
  const identity = useSignedInIdentity();
  const epoch = useEpoch();
  const [form, setForm] = useState<RevokeForm>(EMPTY_FORM);
  // A revoke whose answer was lost blocks the next one until the operator says what happened.
  const [unresolved, setUnresolved] = useState<Unresolved | null>(null);
  useEffect(() => setUnresolved(readUnresolved(epoch, identity)), [epoch, identity]);
  const resolve = (wasRevoked: boolean) => {
    epoch.storage.remove(UNRESOLVED_KEY);
    setUnresolved(null);
    setUnknown(false);
    write.reset();
    if (!wasRevoked) valueRef.current?.focus();
  };
  const latest = useLatest(form);
  // One key per attempt, in memory: the same body keeps its key.
  const attempt = useRef<{ key: string; body: string } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<RevokeBody | null>(null);
  const [result, setResult] = useState<{ count: number; object: string } | null>(null);
  const [unknown, setUnknown] = useState(false);
  const valueRef = useRef<HTMLInputElement>(null);
  const write = useHubWrite<RevokeBody, RevokeResult>({
    path: REVOKE_PATH,
    label: "Revoking the session credential",
    // Operator-only and never approval-parked; nothing is stored for a replay.
    resumable: false,
    formHref: "/settings/credentials",
    idempotencyKey: () => attempt.current?.key ?? newKey("revoke"),
    invalidates: [SETTINGS_KEYS.credentials],
    successMessage: (data) => (data.count > 0 ? `Revoked ${plural(data.count, "session credential")}.` : "No active session credential matched; nothing changed."),
  });
  const selected = SELECTORS.find((s) => s.value === form.selector) ?? SELECTORS[0];
  const fieldId = "revoke-value";

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (unresolved) return;
    const issue = selectorProblem(form.selector, form.value);
    setProblem(issue);
    setResult(null);
    setUnknown(false);
    if (issue) {
      valueRef.current?.focus();
      return;
    }
    const body: RevokeBody = { [form.selector]: form.value.trim() };
    const serialized = JSON.stringify(body);
    if (attempt.current?.body !== serialized) attempt.current = { key: newKey("revoke"), body: serialized };
    setConfirming(body);
  };

  const onConfirm = async () => {
    if (!confirming) return;
    const submitted = { ...latest.current };
    const body = confirming;
    const outcome = await write.run(body);
    setConfirming(null);
    // A lost answer, or a cancel after the request had left: the revoke may
    // have happened, and it cannot be repeated safely, so it stays unknown
    // until the operator settles it. A cancel before sending shows nothing
    // and keeps the form.
    const lost = (outcome.kind === "error" && outcome.outcomeUnknown === true) || cancelledWrite(outcome)?.dispatched === true;
    setUnknown(lost);
    if (lost) {
      const record: Unresolved = { identity: identity ?? null, at: Date.now(), kind: submitted.selector };
      epoch.storage.write(UNRESOLVED_KEY, record);
      setUnresolved(record);
    }
    if (outcome.kind === "done" || outcome.kind === "accepted") {
      setResult({ count: outcome.data.count, object: selected.object(String(Object.values(body)[0])) });
      // Clear only if nothing was typed after the submit.
      const now = latest.current;
      if (now.selector === submitted.selector && now.value === submitted.value) {
        setForm(EMPTY_FORM);
        attempt.current = null;
      }
    }
  };

  const confirmValue = confirming ? String(Object.values(confirming)[0] ?? "") : "";
  return (
    <Section
      id="credentials-revoke"
      title="Stop a session's access"
      description="Revoke the short-lived credential a session on a machine uses to call the hub. Only the operator can do this. The agent's own sign-in is not affected."
    >
      <form noValidate onSubmit={onSubmit} className="flex max-w-xl flex-col gap-3" aria-label="Revoke a session credential">
        <RadioGroup
          legend="What to revoke"
          options={SELECTORS.map((s) => ({ value: s.value, label: s.label, description: s.description }))}
          value={form.selector}
          onChange={(v) => {
            setProblem(null);
            setForm((prev) => ({ ...prev, selector: v as RevokeSelector }));
          }}
        />
        <Field id={fieldId} label={selected.field} hint={selected.hint} error={problem ?? undefined} required>
          <Input
            ref={valueRef}
            value={form.value}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => {
              setProblem(null);
              setForm((prev) => ({ ...prev, value: e.target.value }));
            }}
          />
        </Field>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" variant="danger" disabled={write.isRunning || unresolved !== null}>
            Revoke…
          </Button>
          <span className="text-xs text-[var(--ua-muted)]">For safety, what you type here is not saved if you leave the page.</span>
        </div>
        {unresolved ? (
          <Notice tone="danger" role="alert">
            <p className="m-0 font-medium">We could not confirm whether the credentials were revoked.</p>
            <p className="m-0 mt-1">
              A revoke of {KIND_TEXT[unresolved.kind]} was sent at <time dateTime={new Date(unresolved.at).toISOString()}>{new Date(unresolved.at).toLocaleString()}</time>, but its answer was lost, so it may or may not have
              happened. Another revoke is blocked until you say what happened, because a second one can also stop credentials issued since. Check whether the
              session still reaches the hub (its activity on the{" "}
              <UiLink href="/sessions" className="text-[var(--ua-link)] underline">
                Sessions
              </UiLink>{" "}
              page), then choose:
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              <Button size="sm" variant="secondary" onClick={() => resolve(true)}>
                It was revoked
              </Button>
              <Button size="sm" variant="secondary" onClick={() => resolve(false)}>
                It was not revoked, let me try again
              </Button>
            </div>
          </Notice>
        ) : (
          <WriteFeedback state={write.state} what="The revoke" outcomeUnknown={unknown} />
        )}
        {result && (
          <Notice role="status">
            {result.count > 0
              ? `${result.object}: ${plural(result.count, "credential")} revoked. The session is refused from its next request.`
              : `${result.object}: no active credential matched, so nothing changed. Check the id, or the session may already have ended.`}
          </Notice>
        )}
      </form>
      <ConfirmDialog
        open={confirming !== null}
        variant="danger"
        title="Revoke this session's access?"
        objectName={selected.object(confirmValue)}
        consequence={`${selected.consequence} This cannot be undone: the session has to be started again to get a new credential.`}
        confirmLabel="Revoke"
        busy={write.isRunning}
        onConfirm={() => void onConfirm()}
        onCancel={() => setConfirming(null)}
      />
    </Section>
  );
}
