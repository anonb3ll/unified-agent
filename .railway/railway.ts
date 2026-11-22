// Railway Infrastructure as Code for the hosted hub (task #2108, epic #2107).
//
//   cd .railway && npm install          # once: the `railway` IaC SDK
//   railway link                        # pick the project + environment
//   railway config plan                 # read-only preview
//   railway config apply                # applies after confirmation
//
// A named partial: this file manages only the resources it declares, so an
// apply can never delete anything else in the Railway project.
//
// Runbook, variables and the first-boot token flow: docs/deploy/railway.md.
import { defineRailway, github, preserve, project, service, volume } from "railway/iac";

export const partial = "unified-agent-hub";

export default defineRailway((ctx) => {
  // SQLite/WAL, the vault and service.env live here. One volume, one writer:
  // Railway refuses replicas on a service with a volume, which is the single
  // authoritative replica the cloud plan requires. 5000 MB is the Hobby-plan
  // maximum; raise it on Pro.
  const data = volume("ua-data", { region: "sfo", sizeMB: 5000 });

  const hub = service("unified_agent", {
    source: github("anonb3ll/unified-agent", { branch: "main" }),
    // The repo-root Dockerfile builds the Control Center and the hub together,
    // so UI and API share one origin (same-origin cookies and CSRF).
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 10 },
    healthcheck: "/readyz",
    healthcheckTimeout: 120,
    volumeMounts: { "/data": data },
    env: {
      // The hub binds this port; the generated domain targets it.
      PORT: "8765",
      // Railway volumes are root-owned. The entrypoint starts as root only to
      // take ownership of /data, then drops to uid 10001 before anything runs.
      RAILWAY_RUN_UID: "0",
      // Bearer tokens: a sealed variable set in the dashboard, never in this
      // file. preserve() keeps whatever value Railway already holds.
      UA_AGENT_TOKENS: preserve(),
      // The human operator's login label and name (the label must have a
      // token in UA_AGENT_TOKENS). Set in the dashboard; kept on apply.
      UA_OPERATOR_LABEL: preserve(),
      UA_OPERATOR_NAME: preserve(),
      // Public ingress with WorkOS (task #2114, docs/deploy/railway.md):
      // values are environment-specific and live in the dashboard. The API
      // key and cookie password are sealed secrets.
      UA_EDITION: preserve(),
      UA_HUB_WORKOS_ORGANIZATION_ID: preserve(),
      UA_HUB_WORKOS_IDENTITY_MAP: preserve(),
      WORKOS_ORGANIZATION_ID: preserve(),
      WORKOS_API_KEY: preserve(),
      WORKOS_CLIENT_ID: preserve(),
      WORKOS_ISSUER: preserve(),
      WORKOS_REDIRECT_URI: preserve(),
      WORKOS_COOKIE_PASSWORD: preserve(),
      UA_MCP_OAUTH: preserve(),
      UA_MCP_OAUTH_ISSUER: preserve(),
      // Scheduled offsite backups (task #2585, docs/deploy/railway.md): the
      // R2 bucket, endpoint and bucket-scoped token are set in the dashboard;
      // the two keys are sealed secrets.
      UA_BACKUP_INTERVAL_S: preserve(),
      UA_BACKUP_S3_URI: preserve(),
      UA_BACKUP_S3_ENDPOINT: preserve(),
      UA_BACKUP_S3_ACCESS_KEY_ID: preserve(),
      UA_BACKUP_S3_SECRET_ACCESS_KEY: preserve(),
      // A Hobby volume stops at 5 GB; the bucket keeps the history.
      UA_BACKUP_KEEP_DAILY: "2",
      UA_BACKUP_KEEP_WEEKLY: "0",
    },
  });

  return project(ctx.projectName, { resources: [hub, data] });
});
