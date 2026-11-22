# Self-hosted CI runner (GCP spot VM)

Once the GitHub Actions minute allowance runs out, CI moves to a self-hosted
runner (task #1787). Every job in `.github/workflows/ci.yml` runs on
`vars.UA_CI_RUNNER` when that variable is set, and on GitHub-hosted
`ubuntu-latest` when it is unset. A pull request from a fork
always runs on `ubuntu-latest` and never reaches the self-hosted slots. The
switch is one repo variable and needs no code change:

```bash
gh variable set UA_CI_RUNNER --body ua-ci   # ci.yml jobs go to runners labelled ua-ci
gh variable delete UA_CI_RUNNER             # back to Ubuntu-hosted ubuntu-latest
```

`docker.yml` and `install-gate.yml` follow a separate variable,
`UA_CI_ISOLATED_RUNNER`, and stay on GitHub-hosted runners by default. Docker
access is root on the host, and the install gate installs into `$HOME` and
binds fixed ports, so neither belongs on the shared slots below. Point that
variable only at a dedicated machine.

`setup-runner.sh` builds the runner. It runs no `gcloud` command and mints no
token: those steps belong to the operator.

## Trust model

This is **not a trust boundary**. Run only this private repository's own code
on it, and never fork PRs. `.github/workflows/ci.yml` enforces the second half:
a pull request whose head repository is not this repository always runs on
`ubuntu-latest`, whatever `UA_CI_RUNNER` says.

- **Credentials are root-only.** The registration token and the optional PAT
  live in `/etc/ua-ci-runner` (mode 700, root). Only the root helper that
  systemd runs before each slot starts (`ExecStartPre=+`) reads them. The
  helper registers the slot, passing the token to the runner's `config.sh`
  through root's environment (`ACTIONS_RUNNER_INPUT_TOKEN`), or mints a
  one-job JIT config with the PAT. Job code never sees either credential.
- **Slots are separate users.** Each slot runs as its own user (`ua-ci-1`,
  `ua-ci-2`, …) with no sudo and no Docker group, under `NoNewPrivileges`.
  Slots cannot read each other's runner credentials, homes, caches or tmpfs.
  A job can read its own slot's runner credential: the one-job JIT config in
  ephemeral mode, or the long-lived `.credentials` in persistent mode.
- **What persists.** In ephemeral mode, every job starts with a fresh slot
  directory, a fresh tmpfs and a wiped home, keeping only the Rust toolchain.
  Nothing one job leaves behind reaches the next, apart from that toolchain.
  In persistent mode, a slot's home caches (uv, npm, Playwright) persist
  across its jobs, and a job could poison them for later ones.
- **Still one VM.** The slots share a kernel and a disk. A job that escapes its
  user (a kernel bug, a misconfigured setuid binary) reaches every slot. For a
  real boundary, use one throwaway VM per job. That needs an autoscaler that
  creates a VM per queued job (for example a webhook-driven instance
  template), which this setup does not provide.

## 1. Create the VM

```bash
REPO=<owner>/<repo> PROJECT=<gcp-project> ZONE=us-central1-a \
  deploy/ci-runner/setup-runner.sh gcloud-plan
```

This prints the commands, with your repository filled in, for you to review
and run in any shell:

- a **spot** `e2-standard-8` (8 vCPU, 32 GB) running Ubuntu 24.04. Four slots
  get about 2 vCPU each, like a GitHub-hosted private-repo runner;
- **no service account and no API scopes**. The runner only polls GitHub over
  outbound HTTPS, so the VM holds no Google credentials and needs no inbound
  firewall rule;
- Shielded VM and OS Login;
- `--instance-termination-action=STOP`, so a preempted VM stops and keeps its
  disk. Start it again with `gcloud compute instances start`. A job that was
  running when the VM was preempted fails; re-run it.

## 2. Hand over the credentials

They are yours to mint. The plan pipes the registration token straight from
`gh` into `/root/ua-runner-token` (mode 600) on the VM, so it never appears on
screen, in shell history or in a file you keep. A registration token is valid
for one hour.

For ephemeral slots that run longer than that, also send a fine-grained PAT
scoped to this repository only, with *Administration: read and write*, to
`/root/ua-runner-pat` the same way. Slots use it to mint a JIT config before
every job. Persistent slots register once and need no PAT.

## 3. Install

Pick a runner release from <https://github.com/actions/runner/releases> and
copy its version and the SHA-256 of `actions-runner-linux-x64-<version>.tar.gz`
from the release notes. The script refuses to install a tarball that does not
match. Run the `install` command from the plan. It:

- installs what the CI jobs expect a GitHub-hosted image to provide, because
  the slot users cannot install packages: git, curl, jq, make and a compiler,
  Python 3, bubblewrap, rustup, and Chromium's system libraries for the web
  accessibility gate. It also lifts Ubuntu's AppArmor restriction on
  unprivileged user namespaces, which bubblewrap needs. It installs no Docker;
- moves the credentials into `/etc/ua-ci-runner` and shreds the copies in
  `/root`;
- creates `RUNNER_COUNT` slots (default 4), with labels from `RUNNER_LABELS`
  (default `ua-ci`), each running as the systemd service `ua-ci-runner@<n>`.

Each slot exports `UA_CI_TMPFS` (its own tmpfs under `/run/ua-ci`) to its jobs.
CI's test job puts pytest's temp files there instead of mounting a tmpfs
itself.

## Modes

| `RUNNER_MODE` | Behaviour | Credential it needs |
|---|---|---|
| `ephemeral` (default) | One job per registration. The slot exits after the job, and systemd restarts it with a fresh directory, home and tmpfs. | With the PAT: a JIT config per job, indefinitely. Without it: the registration token, so slots stop registering an hour after install and keep retrying. |
| `persistent` | Each slot registers once and keeps serving jobs; `actions/checkout` cleans the workspace between them. | The registration token, once. |

## Sizing and parallelism

One CI run starts `gates`, then up to four `test` shards plus `js` and
`desktop-core` in parallel. With fewer slots than that, jobs queue and the run
takes longer, but nothing fails.

## Operations

```bash
systemctl status 'ua-ci-runner@*'                 # on the VM
journalctl -u ua-ci-runner@1 -f                   # one slot's log
sudo bash setup-runner.sh uninstall               # stop and disable every slot
gh api repos/$REPO/actions/runners --jq '.runners[] | [.name, .status, .busy] | @tsv'
```

Stale registrations, for example after a preemption, can be removed in
Settings > Actions > Runners. A new registration with the same name replaces
the old one.
