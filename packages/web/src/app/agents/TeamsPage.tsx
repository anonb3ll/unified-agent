/**
 * `/agents/teams` (spec §9.3 Teams; legacy Skills & Teams and Operating).
 * Tabs in the URL (`?tab=teams|registry|operating`, legacy `operating` maps
 * to `?tab=operating`):
 * - Teams: the team specs the hub can start, each with its reviewed plan
 *   (seats, launch order, refusals, warnings) and "Start this team"
 *   (`POST /api/action/team-up` with the plan's hash), its approval step
 *   explained inline.
 * - Registry: teams from the org registry with members, roles, skills and
 *   drift.
 * - Operating: profiles and department bindings (with a draft for the policy
 *   revision that changes them), sprints and per-run settings
 *   (`OperatingPanel.tsx`).
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, CodeBlock, ConfirmDialog, DataTable, DescriptionList, PageHeader, StatePill, Tabs, type Column } from "../../lib/ui";
import { useEpoch } from "../lib/epoch";
import { useHubWrite } from "../lib/mutations";
import { QueryRegion } from "../lib/queries";
import { useUrlParams } from "../lib/urlState";
import { AGENTS_KEYS, fetchSkillsTeams, fetchTeamPlan, fetchTeams, WRITE_PATHS, type TeamSummary, type TeamUpResult } from "./api";
import { OperatingPanel } from "./OperatingPanel";
import { StarterTemplates, TeamInstall } from "./SkillActions";
import { AgentsTabs, Section, shownFrom, useAgentsLiveUpdates, WriteFeedback, type ShownOutcome } from "./shared";

const DEFAULTS = { tab: "teams", team: "" };

export function TeamsPage() {
  useAgentsLiveUpdates();
  const { values, update } = useUrlParams(DEFAULTS);
  const tab = ["teams", "registry", "operating"].includes(values.tab) ? values.tab : "teams";
  return (
    <>
      <PageHeader title="Teams" description="Groups of agents that start and work together, the teams in the org registry, and the operating profiles they run under." tabs={<AgentsTabs />} />
      <Tabs
        label="Teams views"
        value={tab}
        onChange={(id) => update({ tab: id }, { push: true })}
        items={[
          { id: "teams", label: "Start a team", panel: tab === "teams" ? <TeamSpecs selected={values.team} select={(team) => update({ team }, { push: true })} /> : null },
          { id: "registry", label: "Registry teams", panel: tab === "registry" ? <RegistryTeams /> : null },
          { id: "operating", label: "Operating", panel: tab === "operating" ? <OperatingPanel /> : null },
        ]}
      />
    </>
  );
}

const TEAM_COLUMNS: Column<TeamSummary>[] = [
  { id: "team", header: "Team", rowLink: true, cell: (t) => <span className="font-medium">{t.team}</span> },
  { id: "description", header: "What it is for", cell: (t) => t.description ?? "No description" },
  { id: "seats", header: "Seats", cell: (t) => t.seats.map((s) => s.label).join(", ") || "None" },
  { id: "profile", header: "Operating profile", cell: (t) => t.operating_profile ?? "Default" },
];

function TeamSpecs({ selected, select }: { selected: string; select: (team: string) => void }) {
  const teams = useQuery({ queryKey: AGENTS_KEYS.teams, queryFn: ({ signal }) => fetchTeams(signal) });
  return (
    <div className="flex flex-col gap-4">
      <QueryRegion
        query={teams}
        loadingLabel="Loading teams"
        errorTitle="Teams could not be loaded"
        isEmpty={(d) => d.length === 0}
        empty={{ title: "No team specs", description: "A team spec in the hub's configuration appears here, ready to start." }}
      >
        {(data) => (
          <DataTable
            columns={TEAM_COLUMNS}
            data={data}
            getRowKey={(t) => t.team}
            getRowHref={(t) => `/agents/teams?team=${encodeURIComponent(t.team)}`}
            caption="Team specs"
            captionSrOnly
            scrollLabel="Team specs"
          />
        )}
      </QueryRegion>
      {selected && <TeamPlanPanel key={selected} team={selected} onClose={() => select("")} />}
    </div>
  );
}

/** One team's reviewed plan and the start action. Keyed by team: nothing carries over between teams. */
function TeamPlanPanel({ team, onClose }: { team: string; onClose: () => void }) {
  const epoch = useEpoch();
  const plan = useQuery({ queryKey: AGENTS_KEYS.teamPlan(team), queryFn: ({ signal }) => fetchTeamPlan(team, signal) });
  const [confirming, setConfirming] = useState(false);
  const [shown, setShown] = useState<ShownOutcome>({ kind: "none" });
  const [result, setResult] = useState<TeamUpResult | null>(null);
  const write = useHubWrite<{ team: string; plan_sha256: string }, TeamUpResult>({
    path: WRITE_PATHS.teamUp,
    // Not proven to rebuild the identical approval payload: submit again after approval.
    resumable: false,
    invalidates: [AGENTS_KEYS.all],
    label: `Starting ${team}`,
    pendingMessage: false,
  });
  const start = async (sha: string) => {
    setShown({ kind: "none" });
    const outcome = await write.run({ team, plan_sha256: sha });
    epoch.run(() => {
      setConfirming(false);
      setShown(shownFrom(outcome));
      if (outcome.kind === "done" || outcome.kind === "accepted") setResult(outcome.data);
    });
  };
  return (
    <Section title={`Plan for ${team}`} id="team-plan" actions={<Button size="sm" variant="ghost" onClick={onClose}>Close</Button>}>
      <QueryRegion query={plan} loadingLabel="Loading the plan" errorTitle="The plan could not be loaded" headingLevel={3} empty={{ title: "No plan" }}>
        {(p) => (
          <div className="flex flex-col gap-3">
            <p className="m-0 text-sm">
              <StatePill kind={p.ready ? "ok" : "warn"} label={p.ready ? "Ready" : "Not ready"} />{" "}
              {p.ready ? "Every seat can start." : "Some seats cannot start; fix the refusals below first."}
            </p>
            <CodeBlock label="What starting the team does" code={p.plan_text} copyable={false} />
            {p.refusals.length > 0 && (
              <ul aria-label="Refusals" className="m-0 pl-5 text-sm">
                {p.refusals.map((r) => (
                  <li key={`${r.seat}-${r.code}`}>
                    {r.seat}: {r.detail} ({r.code})
                  </li>
                ))}
              </ul>
            )}
            {p.warnings.length > 0 && (
              <ul aria-label="Warnings" className="m-0 pl-5 text-sm text-[var(--ua-warn)]">
                {p.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            )}
            <p className="m-0 text-sm text-[var(--ua-muted)]">
              Starting a team may need approval. If it does, nothing starts until a different person approves it; then start it again from here.
            </p>
            <div>
              <Button variant="primary" disabled={!p.ready || write.isRunning} onClick={() => setConfirming(true)}>
                Start this team
              </Button>
            </div>
            <WriteFeedback state={write.state} shown={shown} what={`Starting ${team}`} checkHref="/sessions?status=open" checkLabel="check open sessions" />
            {result && (
              <div role="status" className="text-sm">
                <p className="m-0">{result.booted ? `${team} started.` : `${team} did not start every seat.`}</p>
                {result.seats && (
                  <ul className="m-0 pl-5">
                    {result.seats.map((s) => (
                      <li key={s.label}>
                        {s.label}: {s.outcome}
                        {s.reason ? ` (${s.reason})` : ""}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            {confirming && (
              <ConfirmDialog
                open
                title={`Start ${team}?`}
                objectName={team}
                consequence="Every seat in the plan starts in its launch order, on its host."
                confirmLabel="Start the team"
                busy={write.isRunning}
                onCancel={() => setConfirming(false)}
                onConfirm={() => void start(p.plan_sha256)}
              />
            )}
          </div>
        )}
      </QueryRegion>
    </Section>
  );
}

function RegistryTeams() {
  const data = useQuery({ queryKey: AGENTS_KEYS.skillsTeams, queryFn: ({ signal }) => fetchSkillsTeams(signal) });
  return (
    <QueryRegion
      query={data}
      loadingLabel="Loading registry teams"
      errorTitle="Registry teams could not be loaded"
      isEmpty={(d) => d.teams.length === 0 && (d.starter_templates ?? []).length === 0}
      empty={{ title: "No teams in the registry", description: "Teams are defined in the org registry." }}
    >
      {(d) => (
        <div className="flex flex-col gap-3">
          <p className="m-0 text-sm text-[var(--ua-muted)]">Installing a team revision restores the office's captured responsibility and membership. Current profile authority and skill bindings apply.</p>
          {d.teams.map((team) => (
            <Section key={team.id} title={team.name} id={`registry-team-${team.id}`} actions={team.drift ? <StatePill kind="warn" label="Drift" /> : undefined}>
              <DescriptionList
                columns={2}
                items={[
                  { id: "lead", term: "Lead", value: team.lead ?? "None" },
                  { id: "resp", term: "Responsible for", value: team.responsibility.join(", ") || "Not recorded" },
                  { id: "lanes", term: "Default queues", value: team.permitted_scope.default_lanes.join(", ") || "None" },
                  { id: "rights", term: "Decision rights", value: team.permitted_scope.decision_rights.join(", ") || "None" },
                ]}
              />
              <p className="m-0 break-all text-xs text-[var(--ua-muted)]">
                {team.version == null ? "Live organization definition (not captured)" : `Captured org revision #${team.version}`} · SHA-256 {team.content_hash}
              </p>
              {team.drift && <p className="m-0 text-sm text-[var(--ua-warn)]">The live organization definition has drifted from the captured revision.</p>}
              {(team.declared_skill_versions ?? []).length > 0 && (
                <p className="m-0 text-xs">Declared skills: {(team.declared_skill_versions ?? []).map((sk) => `${sk.name} v${sk.version}`).join(", ")}</p>
              )}
              {team.declared_skill_versions_error && <p className="m-0 text-sm text-[var(--ua-warn)]">{team.declared_skill_versions_error}</p>}
              <ul aria-label={`${team.name} members`} className="m-0 list-none p-0 text-sm">
                {team.members.map((m) => (
                  <li key={m.label} className="border-t border-[var(--ua-border)] py-1 first:border-t-0">
                    <span className="font-medium">{m.label}</span> · {m.role} · {m.profile}
                    {m.skills.length > 0 ? ` · skills: ${m.skills.map((s) => `${s.name} v${s.version}`).join(", ")}` : ""}
                  </li>
                ))}
              </ul>
              <TeamInstall team={team} enabled={d.permissions.team_install} />
            </Section>
          ))}
          <StarterTemplates templates={d.starter_templates ?? []} canInstall={d.permissions.team_install} canPreview={d.permissions.drift} />
        </div>
      )}
    </QueryRegion>
  );
}
