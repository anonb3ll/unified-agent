/**
 * Skill and team catalog actions (legacy Skills & Teams), all through
 * `POST /api/action/skills-teams`:
 *
 * - `/agents/skills`: check a skill directory against the registry (drift),
 *   install a published skill version into it (optionally replacing a file
 *   only if its current hash matches), and publish a skill after review.
 * - `/agents/teams?tab=registry`: install a captured team revision, and bind
 *   a starter team template to the roster (preview first, then install).
 *
 * The hub decides who may do each (the read's `permissions`); a refusal,
 * including a 200 whose review blocked a publish, is shown in plain words. A
 * parked action changed nothing: submit it again after approval.
 */
import { useMemo, useState, type FormEvent } from "react";
import type { CatalogAction, CatalogActionResult, SkillEntry, StarterParams, StarterTemplate, SurfaceDrift, TeamEntry } from "../../lib/skills-teams/types";
import { Button, Checkbox, CodeBlock, ConfirmDialog, Field, Input, Select } from "../../lib/ui";
import { useEpoch } from "../lib/epoch";
import { useHubWrite } from "../lib/mutations";
import { AGENTS_KEYS, WRITE_PATHS } from "./api";
import { Section, shownFrom, WriteFeedback, type ShownOutcome } from "./shared";

const SHA256 = /^[a-f0-9]{64}$/i;

/** A 200 that is still a refusal: a blocked review, `ok: false` or an error. Null when it succeeded. */
export function catalogRefusal(result: CatalogActionResult | undefined): string | null {
  if (!result || result.pending) return null;
  if (result.blocked || result.ok === false || result.error) {
    const findings = result.review?.findings?.map((f) => f.message).filter(Boolean).join("; ");
    return result.error || result.message || findings || "The skill review blocked publication.";
  }
  return null;
}

/** One catalog write with its inline result. `label` names what the person did. */
function useCatalogWrite(invalidates = [AGENTS_KEYS.skillsTeams, AGENTS_KEYS.teams]) {
  const epoch = useEpoch();
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const [what, setWhat] = useState("");
  const [result, setResult] = useState<{ ok: boolean; text: string; data: CatalogActionResult } | null>(null);
  const write = useHubWrite<CatalogAction, CatalogActionResult>({
    path: WRITE_PATHS.skillsTeams,
    resumable: false,
    invalidates,
    label: "Skill and team catalog change",
    pendingMessage: false,
  });
  const run = async (action: CatalogAction, label: string): Promise<CatalogActionResult | null> => {
    setWhat(label);
    setShown({ kind: "none" });
    setResult(null);
    const outcome = await write.run(action);
    let accepted: CatalogActionResult | null = null;
    epoch.run(() => {
      setShown(shownFrom(outcome));
      if (outcome.kind === "done") {
        const refusal = catalogRefusal(outcome.data);
        setResult({ ok: refusal === null, text: refusal ?? outcome.data?.message ?? `${label}: done.`, data: outcome.data });
        if (refusal === null) accepted = outcome.data;
      }
    });
    return accepted;
  };
  const feedback = (checkHref: string) => (
    <>
      {result && (
        <p role={result.ok ? "status" : "alert"} className={`m-0 text-sm ${result.ok ? "" : "text-[var(--ua-danger)]"}`}>
          {result.text}
        </p>
      )}
      <WriteFeedback state={write.state} shown={shown} what={what} checkHref={checkHref} checkLabel="reload this page" />
    </>
  );
  return { run, feedback, isRunning: write.isRunning, result };
}

// --- /agents/skills ------------------------------------------------------------

/** The skill directory being compared and installed into (URL `surface`), with its drift report. */
export function SurfaceForm({ surface, setSurface, canCheck }: { surface: string; setSurface: (value: string) => void; canCheck: boolean }) {
  const [input, setInput] = useState(surface);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (input.trim()) setSurface(input.trim());
  };
  return (
    <form aria-label="Choose a skill directory" onSubmit={submit} className="flex flex-wrap items-end gap-2">
      <Field label="Skill directory" hint="A workspace directory holding installed skill folders. It is also where Install writes.">
        <Input value={input} autoComplete="off" disabled={!canCheck} onChange={(e) => setInput(e.target.value)} />
      </Field>
      <Button type="submit" disabled={!canCheck || !input.trim()}>
        Check against the registry
      </Button>
      {surface && (
        <Button
          variant="ghost"
          onClick={() => {
            setInput("");
            setSurface("");
          }}
        >
          Clear
        </Button>
      )}
      {!canCheck && <p className="m-0 w-full text-sm text-[var(--ua-muted)]">Your profile cannot check skill directories.</p>}
      {surface && input.trim() !== surface && <p className="m-0 w-full text-sm text-[var(--ua-warn)]">Check the new directory before installing into it.</p>}
    </form>
  );
}

const DRIFT_GROUPS = [
  ["missing", "Missing"],
  ["stale", "Out of date"],
  ["unregistered", "Not in the registry"],
  ["unassigned", "Not assigned"],
  ["synced", "In sync"],
] as const;

export function DriftReport({ surface }: { surface: SurfaceDrift }) {
  const r = surface.result;
  return (
    <Section title={`Installed in ${r.root || surface.surface_dir}`} id={`surface-${surface.surface_dir}`}>
      {r.error ? (
        <p role="alert" className="m-0 text-sm text-[var(--ua-warn)]">
          Could not compare: {r.error}
        </p>
      ) : (
        <>
          <p className="m-0 text-sm text-[var(--ua-muted)]">Compared with {r.comparison === "resolved_assignments" ? "the skill versions assigned to agents" : "the latest published versions"}.</p>
          <dl className="m-0 grid gap-2 sm:grid-cols-2">
            {DRIFT_GROUPS.map(([group, label]) => (
              <div key={group}>
                <dt className="text-sm font-medium">
                  {label}: {r[group]?.length ?? 0}
                </dt>
                <dd className="m-0 text-xs">
                  {(r[group] ?? []).map((row) => (
                    <p key={row.name} className="m-0 break-all">
                      {row.name}
                      {row.registry_version != null ? ` · registry v${row.registry_version}` : row.version != null ? ` · v${row.version}` : ""}
                      {row.on_disk_hash ? ` · current SHA-256 ${row.on_disk_hash}` : ""}
                      {row.registry_hash ? ` · registry SHA-256 ${row.registry_hash}` : ""}
                    </p>
                  ))}
                </dd>
              </div>
            ))}
          </dl>
        </>
      )}
    </Section>
  );
}

/** Install one published skill version into the checked directory. */
function SkillInstall({ versions, surface, enabled }: { versions: SkillEntry[]; surface: string; enabled: boolean }) {
  const [version, setVersion] = useState(versions[0].version);
  const [replace, setReplace] = useState(false);
  const [expected, setExpected] = useState("");
  const write = useCatalogWrite([AGENTS_KEYS.skillsTeams]);
  const selected = versions.find((v) => v.version === version) ?? versions[0];
  const hashOk = !replace || SHA256.test(expected.trim());
  const ready = enabled && selected.installable === true && Boolean(surface) && hashOk && !write.isRunning;
  const install = (event: FormEvent) => {
    event.preventDefault();
    if (!ready) return;
    void write.run(
      {
        action: "install",
        params: {
          name: selected.name,
          version: selected.version,
          content_hash: selected.content_hash,
          surface_dir: surface,
          ...(replace ? { expected_hash: expected.trim().toLowerCase() } : {}),
        },
      },
      `Installing ${selected.name} v${selected.version}`,
    );
  };
  return (
    <form aria-label={`Install ${selected.name}`} onSubmit={install} className="flex flex-col gap-2 border-t border-[var(--ua-border)] pt-3 first:border-t-0 first:pt-0">
      <h3 className="m-0 text-sm font-semibold">{selected.name}</h3>
      <Field label={`Version of ${selected.name}`}>
        <Select value={String(selected.version)} onChange={(e) => setVersion(Number(e.target.value))} options={versions.map((v) => ({ value: String(v.version), label: `v${v.version}${v.latest ? " (latest)" : ""}` }))} />
      </Field>
      <p className="m-0 break-all text-xs">
        SHA-256 <code>{selected.content_hash}</code> · published by {selected.provenance?.actor || selected.published_by} from {selected.provenance?.source || selected.source}
      </p>
      <p className="m-0 break-all text-sm text-[var(--ua-muted)]">Writes to {surface ? `${surface}/${selected.name}/SKILL.md` : "the checked skill directory (check one above first)"}.</p>
      <Checkbox
        label={`Replace an existing ${selected.name}/SKILL.md only if its current hash matches`}
        checked={replace}
        disabled={!enabled}
        onChange={(e) => setReplace(e.target.checked)}
      />
      {replace && (
        <Field label={`Current SHA-256 of ${selected.name}/SKILL.md`} hint="From the drift report above." error={expected.trim() && !hashOk ? "A SHA-256 is 64 hexadecimal characters." : undefined}>
          <Input value={expected} autoComplete="off" className="font-mono" onChange={(e) => setExpected(e.target.value)} />
        </Field>
      )}
      <div>
        <Button type="submit" size="sm" disabled={!ready} loading={write.isRunning}>
          Install {selected.name} v{selected.version}
        </Button>
      </div>
      {selected.installable !== true && <p className="m-0 text-sm text-[var(--ua-warn)]">{selected.install_block_reason || "This version cannot be installed as a standalone SKILL.md."}</p>}
      {write.feedback("/agents/skills")}
    </form>
  );
}

export function SkillInstallSection({ skills, surface, enabled }: { skills: SkillEntry[]; surface: string; enabled: boolean }) {
  const groups = useMemo(() => {
    const byName = new Map<string, SkillEntry[]>();
    for (const s of skills) byName.set(s.name, [...(byName.get(s.name) ?? []), s]);
    return [...byName].sort(([a], [b]) => a.localeCompare(b)).map(([name, rows]) => ({ name, rows: [...rows].sort((a, b) => b.version - a.version) }));
  }, [skills]);
  return (
    <Section title="Install a skill" id="skills-install" description="Write a published skill version into the checked directory.">
      {!enabled && <p className="m-0 text-sm text-[var(--ua-muted)]">Your profile cannot install skills.</p>}
      {groups.length === 0 ? <p className="m-0 text-sm">No skills have been published.</p> : groups.map((g) => <SkillInstall key={g.name} versions={g.rows} surface={surface} enabled={enabled} />)}
    </Section>
  );
}

export function SkillPublishSection({ enabled }: { enabled: boolean }) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const write = useCatalogWrite([AGENTS_KEYS.skillsTeams]);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!enabled || !name.trim() || !path.trim()) return;
    const submitted = { name: name.trim(), path: path.trim() };
    const done = await write.run({ action: "publish", params: submitted }, `Publishing ${submitted.name}`);
    if (done) {
      setName((now) => (now.trim() === submitted.name ? "" : now));
      setPath((now) => (now.trim() === submitted.path ? "" : now));
    }
  };
  return (
    <Section title="Publish a skill" id="skills-publish" description="Publishing runs a skill review first; a high-severity finding blocks it.">
      <form aria-label="Publish a skill" onSubmit={(e) => void submit(e)} className="flex flex-col gap-2">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Skill name">
            <Input value={name} disabled={!enabled} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Skill source path" hint="A workspace skill directory or its SKILL.md.">
            <Input value={path} autoComplete="off" disabled={!enabled} onChange={(e) => setPath(e.target.value)} />
          </Field>
        </div>
        <div>
          <Button type="submit" size="sm" variant="primary" disabled={!enabled || !name.trim() || !path.trim()} loading={write.isRunning}>
            Review and publish
          </Button>
        </div>
        {!enabled && <p className="m-0 text-sm text-[var(--ua-muted)]">Your profile cannot publish skills.</p>}
        {write.feedback("/agents/skills")}
      </form>
    </Section>
  );
}

// --- /agents/teams?tab=registry -----------------------------------------------

/** Install one captured team revision (restores the office's responsibility and membership). */
export function TeamInstall({ team, enabled }: { team: TeamEntry; enabled: boolean }) {
  const [confirming, setConfirming] = useState(false);
  const write = useCatalogWrite();
  const revision = team.version;
  if (revision == null) return <p className="m-0 text-sm text-[var(--ua-muted)]">Capture an organization revision before installing this team.</p>;
  if (!enabled) return <p className="m-0 text-sm text-[var(--ua-muted)]">Installing a team needs organization revision authority.</p>;
  return (
    <div className="flex flex-col gap-2">
      <div>
        <Button size="sm" disabled={write.isRunning} onClick={() => setConfirming(true)}>
          Install {team.name} revision #{revision}
        </Button>
      </div>
      {write.feedback("/agents/teams?tab=registry")}
      {confirming && (
        <ConfirmDialog
          open
          title={`Install ${team.name} revision #${revision}?`}
          objectName={`${team.name} #${revision}`}
          consequence="The office's responsibility and membership are restored from this revision. Current profile authority and skill bindings still apply."
          confirmLabel="Install team"
          busy={write.isRunning}
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            setConfirming(false);
            void write.run({ action: "team_install", params: { team_id: team.id, revision, content_hash: team.content_hash } }, `Installing ${team.name} revision #${revision}`);
          }}
        />
      )}
    </div>
  );
}

const HARNESSES = ["codex", "claude", "gemini", "cursor", "copilot", "opencode", "hermes"];
const TRUST_PRESETS = ["inspection", "configured-workspace", "active-worktree"];

function StarterCard({ template, canInstall, canPreview }: { template: StarterTemplate; canInstall: boolean; canPreview: boolean }) {
  const [team, setTeam] = useState("");
  const [profile, setProfile] = useState("");
  const [preset, setPreset] = useState("active-worktree");
  const [bindings, setBindings] = useState<StarterParams["bindings"]>(() => Object.fromEntries(Object.keys(template.roles).map((role) => [role, { label: "", harness: "codex" }])));
  const [previewed, setPreviewed] = useState("");
  const write = useCatalogWrite();
  const params: StarterParams = { template_id: template.id, team_id: team.trim(), operating_profile: profile.trim(), trust_preset: preset, content_hash: template.content_hash, bindings };
  const fingerprint = JSON.stringify(params);
  const complete = Boolean(team.trim() && profile.trim()) && Object.values(bindings).every((b) => b.label.trim());
  const preview = async () => {
    setPreviewed("");
    const done = await write.run({ action: "starter_preview", params }, `Previewing ${template.id}`);
    if (done) setPreviewed(fingerprint);
  };
  return (
    <section aria-label={`Starter ${template.id}`} className="flex flex-col gap-3 rounded-md border border-[var(--ua-border)] p-3">
      <h3 className="m-0 text-sm font-semibold">
        {template.id} · v{template.version}
      </h3>
      <p className="m-0 text-sm">{template.description}</p>
      {template.guidance && <p className="m-0 text-sm text-[var(--ua-muted)]">{template.guidance}</p>}
      <p className="m-0 break-all text-xs">SHA-256 {template.content_hash}</p>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Existing office id">
          <Input value={team} onChange={(e) => setTeam(e.target.value)} />
        </Field>
        <Field label="Existing operating profile">
          <Input value={profile} onChange={(e) => setProfile(e.target.value)} />
        </Field>
        <Field label="Existing trust preset">
          <Select value={preset} onChange={(e) => setPreset(e.target.value)} options={TRUST_PRESETS.map((p) => ({ value: p, label: p }))} />
        </Field>
      </div>
      <p className="m-0 text-sm text-[var(--ua-muted)]">Every seat must already have the chosen trust preset. Installing records the binding and grants no permissions.</p>
      {Object.entries(template.roles).map(([role, guidance]) => (
        <fieldset key={role} className="m-0 flex flex-col gap-2 rounded border border-[var(--ua-border)] p-2">
          <legend className="px-1 text-sm font-medium">{role}</legend>
          <p className="m-0 text-sm text-[var(--ua-muted)]">{guidance}</p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={`${role} roster label`}>
              <Input value={bindings[role].label} onChange={(e) => setBindings({ ...bindings, [role]: { ...bindings[role], label: e.target.value } })} />
            </Field>
            <Field label={`${role} harness`}>
              <Select value={bindings[role].harness} onChange={(e) => setBindings({ ...bindings, [role]: { ...bindings[role], harness: e.target.value } })} options={HARNESSES.map((h) => ({ value: h, label: h }))} />
            </Field>
          </div>
        </fieldset>
      ))}
      {template.independent_roles.length > 0 && <p className="m-0 text-sm">These roles must use different harnesses: {template.independent_roles.join(", ")}.</p>}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" disabled={!canPreview || !complete || write.isRunning} onClick={() => void preview()}>
          Preview {template.id}
        </Button>
        <Button
          size="sm"
          variant="primary"
          disabled={!canInstall || !complete || previewed !== fingerprint || write.isRunning}
          onClick={() => void write.run({ action: "starter_install", params }, `Installing ${template.id}`)}
        >
          Install {template.id}
        </Button>
      </div>
      {!canInstall && <p className="m-0 text-sm text-[var(--ua-muted)]">Installing needs configuration revision authority.</p>}
      {canInstall && complete && previewed !== fingerprint && <p className="m-0 text-sm text-[var(--ua-muted)]">Preview these exact choices before installing.</p>}
      {write.feedback("/agents/teams?tab=registry")}
      {write.result?.ok && <CodeBlock label="Preview or installation result" code={JSON.stringify(write.result.data, null, 2)} language="json" copyable={false} />}
    </section>
  );
}

export function StarterTemplates({ templates, canInstall, canPreview }: { templates: StarterTemplate[]; canInstall: boolean; canPreview: boolean }) {
  if (templates.length === 0) return null;
  return (
    <Section
      title="Starter team templates"
      id="starter-templates"
      description="Bind a reviewed template to your roster, preview whether it is ready, then install it as a revision. Start the installed office from Start a team."
    >
      {templates.map((t) => (
        <StarterCard key={`${t.id}:${t.content_hash}`} template={t} canInstall={canInstall} canPreview={canPreview} />
      ))}
    </Section>
  );
}
