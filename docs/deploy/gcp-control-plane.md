# Cloud control plane on a GCP VM

Task #2148, epic #2107. On 2026-10-17 the owner decided that a Compute Engine VM
is the **production** home of the cloud control plane, with Railway kept as
the reference and staging deployment ([railway.md](railway.md)). This page is
the runbook for the GCP host. The Mac hub stays authoritative until the
Phase-5 cutover; standing this host up does not move any authority.

The deployment artifact is the one Railway runs: the repo `Dockerfile` image,
state on a mounted disk at `/data`, and the same env contract. Nothing here
forks that path. The only image change this host needed is host-neutral and
backward compatible: `UA_STATE_MOUNT_PATH` gives a non-Railway host the same
"refuse to start without persistent state" guard that `RAILWAY_VOLUME_MOUNT_PATH`
gives Railway.

## Shape

| Piece | Value |
|---|---|
| Project / region | the existing `ua-worker` project, `us-central1-a`, beside the worker VM |
| VM | `ua-control-plane`, `e2-medium` (2 vCPU, 4 GB), Debian 12, Shielded VM, deletion protection, **no external IP** (outbound through the existing Cloud NAT) |
| Boot disk | 30 GB `pd-balanced` (OS, docker, image build cache) |
| Data disk | `ua-control-plane-data`, 64 GB `pd-standard` (50-100 allowed), separate from boot, `auto-delete=no`, mounted at `/mnt/ua-data` and bound to `/data` in the container |
| Snapshots | resource policy `ua-control-plane-daily`: daily 03:00 UTC, 14 days, regional, kept if the disk is deleted |
| Offsite backup | daily 02:30 UTC (plus at most 5 minutes of jitter, capped at 20 minutes, so it always ends before the 03:00 snapshot and each snapshot holds that day's copy): the image's backup runner writes a verified `memory.db` snapshot to `/data/backups`, then `sync-backup-to-gcs.sh` copies it to `gs://<backup bucket>/memory-db/ua-control-plane/` |
| Service account | `ua-backup-uploader` (bucket-scoped object create/view only); no key file on the VM |
| Ingress | hub publishes on `127.0.0.1:8765` only. Tailscale Serve forwards tailnet `:8765` (TCP, the agents' MCP URL) and `:443` (HTTPS, Control Center and OAuth), exactly as on the Mac. Funnel is never enabled |
| Firewall | the existing tag-scoped rules only: Tailscale UDP 41641, SSH from the IAP range. No app or MCP port is opened |
| Execution | `UA_EXECUTION_LOCALITY=devices`, pinned by the unit: no agent runs on the hub ([execution-locality.md](execution-locality.md)) |

Files: [`deploy/gcp/control-plane/`](../../deploy/gcp/control-plane/).

| File | Runs on | Role |
|---|---|---|
| `bootstrap.sh` | operator desktop | Creates the snapshot policy, data disk and VM; checks the firewall; provisions over IAP SSH; runs the verify. **Dry-run by default** |
| `install.sh` | VM, as root | Formats the data disk only if blank, mounts it, installs docker and Tailscale, joins the tailnet, renders the env files, builds or pulls the image, installs the units, sets up Tailscale Serve. Dry-run without `--apply` |
| `hub.env.template` | operator desktop | The env contract with no values (the Railway service variables) |
| `ua-hub.service` | VM | `docker run` of the image with the data disk at `/data`, loopback publish, pinned hosted posture |
| `ua-hub-backup.service`, `.timer`, `backup.sh` | VM | Daily snapshot and GCS copy |
| `verify.sh` | VM | Acceptance checks; `--restart` adds restart-retains-state |

## Before the first run

These are the owner's steps. None of them is automated.

1. **Approve the spend.** The plan creates billable resources: an `e2-medium`
   VM, a 64 GB standard disk, a 30 GB balanced boot disk and daily snapshots.
   Preview the exact commands with the dry-run below.
2. **Fill the env file** outside the repo, mode 0600:

   ```bash
   install -D -m 600 deploy/gcp/control-plane/hub.env.template ~/.config/unified-agent/gcp-hub.env
   ```

   Fill it as you would the Railway variables (tokens, operator label,
   WorkOS). A value may be a Secret Manager reference
   (`secretmanager:<secret-id>`); that needs the Secret Manager API enabled
   and `roles/secretmanager.secretAccessor` for `ua-backup-uploader` on that
   secret, both IAM changes this repo does not make. `WORKOS_REDIRECT_URI`
   and `UA_MCP_OAUTH_ISSUER` use the VM's MagicDNS name,
   `https://ua-control-plane.<tailnet>.ts.net`.
3. **Mint a one-time Tailscale auth key** (admin console, single use,
   pre-approved) and save it to a mode 0600 file outside the repo. It is
   shredded on the VM after the join.
4. **Pick the commit.** The pinned checkout is `git archive` of a commit on
   `main`, the same tree Railway builds from. `git fetch origin` first.

## Run it

Preview (read-only: it runs only `describe` calls):

```bash
UA_GCP_PROJECT=ua-worker deploy/gcp/control-plane/bootstrap.sh \
  --ref=origin/main --env-file=$HOME/.config/unified-agent/gcp-hub.env \
  --tailscale-authkey-file=$HOME/.config/unified-agent/ts-authkey
```

Apply (the one scripted run):

```bash
UA_GCP_PROJECT=ua-worker deploy/gcp/control-plane/bootstrap.sh --apply \
  --ref=origin/main --env-file=$HOME/.config/unified-agent/gcp-hub.env \
  --tailscale-authkey-file=$HOME/.config/unified-agent/ts-authkey
```

`--infra-only` stops after the VM exists. `--image=<ref>` deploys a
published image instead of building the pinned checkout on the VM; the VM's
service account then also needs Artifact Registry read access (an IAM grant).
`--backup-gcs-uri=none` keeps backups local to the disk.

Automation SSH goes through the IAP tunnel with a file key and
`IdentityAgent none` (`--ssh-key-file`, default gcloud's
`~/.ssh/google_compute_engine`). The desktop's interactive `gcp` alias uses the
1Password agent and fails non-interactively, so the script never uses it.

Re-running is safe: existing resources are kept, an existing tailnet login is
kept, and the hub restarts once to pick up a new image or env file. The data
disk is formatted only on the run that creates it, and only when `blkid`,
`wipefs` and `lsblk` all agree it is blank; any probe error or foreign
signature stops the install with nothing formatted. If that first run stopped
before the VM-side install, re-run with `--init-data-disk`. A failed `hub.env`
render leaves the previous file in place. `--backup-gcs-uri=none` on a later
run removes the stored offsite destination.

The staged inputs (`/root/ua-stage`: `hub.env`, the tailnet key, the source
tarball) are deleted whether the install succeeds or fails: `install.sh
--cleanup-stage` removes them on exit, and `bootstrap.sh` removes them itself
if it stops before the install starts. Every value that reaches the VM's root
shell (image ref, backup URI, git ref, project) is checked against a strict
allowlist before any GCP call and quoted with `printf %q`.

`install.sh` keeps the `gcloud` that GCE Debian images ship, and otherwise
installs `google-cloud-cli` from Google's apt repository with a dedicated
signed-by keyring. The offsite upload and Secret Manager references need it;
`verify.sh` checks that it is present. To update the host to a new commit,
run the same apply command with the new `--ref`.

## Verify

`bootstrap.sh` ends with `verify.sh --restart` on the VM. Run it again any
time (without `--restart` it changes nothing):

```bash
gcloud compute ssh ua-control-plane --project=ua-worker --zone=us-central1-a \
  --tunnel-through-iap --ssh-flag=-oIdentityAgent=none \
  --command='sudo /usr/local/lib/unified-agent/verify.sh'
```

It checks, printing PASS or FAIL and no secret values:

- `ua-hub.service` active, the data disk mounted and bound at `/data`;
- `/healthz` 200, `/readyz` 200 with `"ok": true`, unauthenticated `POST /mcp` 401;
- port 8765 listens on loopback and the tailnet only, the VM has no external IP;
- Tailscale Serve carries `:8765` and `:443`, Funnel is off, and `/healthz`
  answers through the MagicDNS name on both;
- `unified-agent doctor` in the container: `execution_locality` LIVE and
  nothing MISCONFIGURED (`hosted_local_dependencies` DEGRADED is expected, as
  on Railway: the image has no checkout, vault or bridge);
- a harness launch from a process with the hub's environment is refused;
- with `--restart`: a nonce on the disk, `service.env` and the `tasks` and
  `memory` row counts survive a service restart.

From a tailnet device, the authenticated checks are the Railway ones with the
MagicDNS origin: `unified-agent verify --hub-url http://ua-control-plane.<tailnet>.ts.net:8765/mcp`
and the WorkOS sign-in checklist in [railway.md](railway.md#verification-checklist).

## Restarts and unit hardening

A stop or restart drains the hub; it is not killed first. `ExecStop` runs
`docker stop -t 30 ua-hub`: SIGTERM to the hub (the entrypoint `exec`s it, so
it is PID 1 in the container), then SIGKILL only if it is still running after
30 s. The hub's own shutdown is shorter than that: uvicorn stops accepting,
waits up to `UA_HTTP_GRACEFUL_SHUTDOWN_S` (default 10 s, at most 25 s) for
open MCP streams, then runs its cleanup. `TimeoutStopSec=45` lets the whole
30 s grace finish before systemd gives up on the stop.

Every start does the same drain for a container left over from an earlier run
(a docker CLI that died while its container kept running, or one started by
hand): `docker stop -t 30 ua-hub`, then `docker rm ua-hub`, both allowed to
fail when there is no container. `TimeoutStartSec=180` covers that grace. The
old `docker rm -f` step SIGKILLed such a container with requests in flight.

Both units run as root, which the docker CLI needs for `/run/docker.sock`, and
carry the hardening that leaves the socket, the data disk and root's home
alone: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full` (`/usr`, `/boot`,
`/efi` and `/etc` read-only), `ProtectKernelTunables`, `ProtectKernelModules`,
`ProtectControlGroups` and `RestrictSUIDSGID`. It applies to the docker CLI
and `backup.sh`, not to the hub: dockerd creates the container outside the
unit's namespaces. `ProtectSystem=strict` and `ProtectHome` are left off on
purpose: gcloud (the backup upload, and docker's `docker-credential-gcloud`
helper for an Artifact Registry `--image`) writes under `/root/.config/gcloud`.

## GCP vs Railway, side by side

The Phase-5 cutover host is chosen on two recorded runs, not on preference
(decision 2026-10-17). Fill both columns from live evidence.

| Item | GCP VM (production candidate) | Railway (staging) |
|---|---|---|
| Artifact | repo `Dockerfile` at `<sha>` (built on the VM, or `--image`) | repo `Dockerfile` from `main` |
| State | 64 GB PD at `/data`, guarded by `UA_STATE_MOUNT_PATH` | 5 GB volume at `/data`, guarded by `RAILWAY_VOLUME_MOUNT_PATH` |
| Env contract | `/etc/unified-agent/hub.env` (0600) + derived `host.env` | service variables (`.railway/railway.ts`) |
| Hosted flag | `UA_HOSTED=1`, pinned by the unit | `RAILWAY_ENVIRONMENT_ID` |
| Execution locality | `devices`, pinned | `devices` (hosted default) |
| Ingress | tailnet only: Tailscale Serve `:8765` TCP and `:443` HTTPS | public HTTPS on the Railway domain |
| Human sign-in | WorkOS on the MagicDNS origin | WorkOS on the Railway domain |
| Snapshots | daily PD snapshots, 14 days | none automatic |
| Offsite backup | daily `memory.db` copy to the fleet GCS bucket | not yet restore-tested |
| Restart policy | systemd `Restart=on-failure` | dashboard setting (IaC policy did not persist) |
| Host access | IAP SSH with a file key | `railway ssh` (needs an account key) |
| `healthz` / `readyz` 200 | ☐ | ☐ |
| Unauthenticated `/mcp` 401; authenticated MCP reachable | ☐ | ☐ |
| `system_doctor`: `execution_locality` LIVE | ☐ | ☐ |
| Harness launch refused on the hub | ☐ | ☐ |
| Restart retains state | ☐ | ☑ (2026-10-16) |
| WorkOS sign-in and device enrollment | ☐ | ☐ |
| #2112 device acceptance, Linux device | ☐ | ☐ |
| #2112 device acceptance, Mac device | ☐ | ☐ |
| Deploy-log row and `vault_journal` line | ☐ | ☐ |

## Not covered here

- **Creating the resources.** This page and the scripts prepare the run;
  the owner runs `--apply`.
- **The #2112 device acceptance run** against this host, and the deploy-log
  row and journal line that record it.
- **Cut-over from the Mac hub** (Phase 5). The Mac stays authoritative until
  it happens; this host does not take writes from the fleet before then.
