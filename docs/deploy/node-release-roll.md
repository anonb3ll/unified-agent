# Node release roll

How a host's `ua-node` moves to new code, how to roll it back, and how old
release worktrees are retired. The procedure is `scripts/node-release-roll.sh`
(task #2298).

## The rule

A node supervisor names exactly one path: the link
`~/.unified_agent/release/current`. The systemd unit `ua-node` (Linux) and the
LaunchAgent `com.unified-agent.node` (macOS) run
`~/.unified_agent/release/current/.venv/bin/unified-agent node` and never a
versioned `release-<date>-<sha8>` directory.

The link's target is never a cleanup candidate. Neither is
`~/.unified_agent/release/previous`, the rollback target the script records.
Idle worktree cleanup (`tools/worktree_policy.py`) skips both targets whatever
their names, as well as any `release-*` or `rollback-*` worktree.

Supervisors pinned to a versioned path failed in three ways:

1. A unit backup named a release worktree that no longer existed, so rolling
   back to it was impossible.
2. Worktree hygiene could not tell a pinned release from an abandoned branch.
   It deleted a live release worktree, and `ua-node` went into a `203/EXEC`
   restart loop (task #2292).
3. `systemctl --user cat` showed the templated path while a drop-in overrode
   it, so the live path did not appear in the unit file.

The repo templates name the link (task #2322):
`deploy/systemd/ua-node.service` runs
`%h/.unified_agent/release/current/.venv/bin/unified-agent node`, and
`deploy/mac-hub/com.unified-agent.node.plist` runs
`__HOME__/.unified_agent/release/current/.venv/bin/unified-agent node` with
`UA_CODE_ROOT` set to the link. `__HOME__` is the account's home: launchd
expands neither `~` nor `$HOME`, so the plist is rendered at install with
`deploy/mac-hub/render_launchagent.py <template> <installed plist>` and
never copied verbatim.

On the Mac, `~/dev/ua-current` is the hub's release link, which
`deploy-mac-hub.sh --atomic` manages (see
mac-hub-topology.md). The other Mac
LaunchAgent templates use it. Do not point the node at it; the node uses
`~/.unified_agent/release/current`.

## A new host

Seed the link with the checkout before installing the unit, so the supervisor
never execs a missing path. The first roll moves it to a release worktree.

```bash
mkdir -p ~/.unified_agent/release
[ -e ~/.unified_agent/release/current ] || ln -s ~/dev/unified_agent ~/.unified_agent/release/current
```

On the Linux desktop, `deploy/desktop-bootstrap.sh --apply` does this, and it
installs `ua-node.service` only once the link has a `.venv/bin/unified-agent`.
Then install the unit as its header comment says (systemd: `cp`; macOS: the
`render_launchagent.py` above, into `~/Library/LaunchAgents/`).

## Reinstall from the template (once per existing host, after #2322)

Hosts installed before #2322 run a unit that names the checkout or
`~/dev/ua-current`, possibly with the drop-in below on top. Reinstall the unit
from a checkout that contains #2322. Nothing here runs automatically. First,
`scripts/node-release-roll.sh --check-only` must show a link whose target
exists; seed it as above if there is none.

Linux (the desktop, and any other systemd node):

```bash
cd ~/dev/unified_agent
cp ~/.config/systemd/user/ua-node.service ~/.config/systemd/user/ua-node.service.bak-2322-$(date +%Y%m%d%H%M%S)
cp deploy/systemd/ua-node.service ~/.config/systemd/user/ua-node.service
systemctl --user daemon-reload
systemctl --user show -p ExecStart --value ua-node   # must name .unified_agent/release/current
scripts/node-release-roll.sh --check-only            # supervisor names release/current: yes
```

A `10-release-current.conf` drop-in (below) sets the same `ExecStart`, so with
it in place the effective command does not change and no restart is needed. It
is now redundant; keep it, or rename it with a timestamped suffix and
`daemon-reload` again. Without the drop-in, the running node still has the old
path: `systemctl --user restart ua-node`. On the desktop,
`deploy/desktop-bootstrap.sh --apply` installs the unit and restarts an active
`ua-node` in one step.

macOS (from a normal login shell; see the launchd constraint below):

```bash
cd ~/dev/unified_agent
la=~/Library/LaunchAgents/com.unified-agent.node.plist
new="$(mktemp)"
python3 deploy/mac-hub/render_launchagent.py deploy/mac-hub/com.unified-agent.node.plist "$new"
plutil -lint "$new"
diff "$la" "$new"   # keep any host-only key the installed copy carries
cp -p "$la" "$la.bak-2322-$(date +%Y%m%d%H%M%S)"
mv "$new" "$la"
launchctl bootout "gui/$(id -u)/com.unified-agent.node"
launchctl bootstrap "gui/$(id -u)" "$la"
scripts/node-release-roll.sh --check-only   # supervisor names release/current: yes
```

`launchctl kickstart -k` is not enough: launchd reads a plist only at
`bootstrap`.

## Point an installed unit at the link with a drop-in

Before #2322 this was the once-per-host step. It still works on a host that
keeps an older unit file.

Linux: add this drop-in, then disable any older `50-release-*.conf` drop-in by
renaming it with a timestamped suffix.

```ini
# ~/.config/systemd/user/ua-node.service.d/10-release-current.conf
[Service]
ExecStart=
ExecStart=%h/.unified_agent/release/current/.venv/bin/unified-agent node
```

```bash
systemctl --user daemon-reload
systemctl --user show -p ExecStart --value ua-node   # must name .unified_agent/release/current
```

The empty `ExecStart=` line clears the unit's own value. Without it, systemd
refuses a second `ExecStart` for a `Type=simple` service. Check the effective
value with `systemctl --user show`, not `systemctl --user cat`, which prints
every fragment without saying which one wins.

macOS: in `~/Library/LaunchAgents/com.unified-agent.node.plist`, set
`ProgramArguments[0]` to
`/Users/<user>/.unified_agent/release/current/.venv/bin/unified-agent`. launchd
does not expand `~`, so use an absolute path. launchd reads a plist only at
`bootstrap`, so run `launchctl bootout gui/$(id -u)/com.unified-agent.node`,
then `launchctl bootstrap gui/$(id -u) <plist>`, from a normal login shell (see
the launchd constraint below).

`--check-only` reports whether the supervisor names the link. A roll and
`--retire-stale` refuse to run until it does: swapping the link changes nothing
while the supervisor runs a versioned path. The check compares the effective
program with this host's exact path,
`$HOME/.unified_agent/release/current/.venv/bin/unified-agent`. A
`release/current` link under another directory does not pass.

## Roll

```bash
scripts/node-release-roll.sh --check-only            # current state, writes nothing
scripts/node-release-roll.sh --sha <sha> --dry-run   # every step, writes nothing
scripts/node-release-roll.sh --sha <sha>
```

`<sha>` must already be in the local object store (`git fetch` first). The
script resolves the main checkout from `--repo` (default: the checkout that
holds the script), so it works when run from inside a release worktree. Steps:

1. `git worktree add --detach .worktrees/release-<YYYYMMDD>-<sha8> <sha>` in the
   main checkout. If a release worktree for that commit already exists, it is
   reused, unless tracked files in it have uncommitted changes; then the roll
   is refused. If that worktree is already the link's target, the roll is
   complete only once the node passes the health check below. Otherwise the
   script restarts the node once and fails if it is still unhealthy.
2. `uv sync --all-extras` in the new worktree.
3. Gate, with no side effects. `.venv/bin/python -I` must import
   `unified_agent` from inside the new directory, and
   `importlib.metadata.version('unified-agent')` must succeed. The gate never
   runs `unified-agent --version`: on a hub-client host that exits 1 with
   `LiveStoreError` by design (task #2297). If the gate fails, nothing is
   switched.
4. Swap the link with one `rename(2)`: `ln -sfn` to a temporary name, then
   `mv -T` (GNU) or `mv -h` (BSD/macOS). A reader always sees the old target or
   the new one.
5. Restart: `systemctl --user restart ua-node` on Linux, or
   `launchctl kickstart -k gui/$UID/com.unified-agent.node` on macOS. The node
   must be running within `UA_ROLL_WAIT_SECONDS` (default 30), and on the same
   pid `UA_ROLL_SETTLE_SECONDS` later (default 15, longer than the unit's
   `RestartSec=10`, so a crash loop is caught).
6. If the restart or the check fails, the script points the link back at the
   previous target, restarts again, and exits non-zero. On success, it records
   the old target as `release/previous`.
7. Restart this host's idle executors onto the new release. See
   [Executors](#executors). This step runs after the node roll is final and
   never undoes it.

Before the swap, the script records the old and new targets in
`~/.unified_agent/release/.roll.txn`. It removes that record only once the roll
has finished or been undone. If the roll gets SIGINT, SIGTERM or SIGHUP after
the swap, the script rolls back before exiting. If a run is killed outright,
the next roll finds the record and restores the old target, restarts the node,
and then continues. `--check-only` reports a pending record, and
`--retire-stale --yes` refuses while one exists.

Rolls and `--retire-stale --yes` take one lock,
`~/.unified_agent/release/.roll.lock`, before reading any link. That way a
retirement cannot remove a worktree that a concurrent roll is making current.
The lock records its owner's pid. A lock whose pid is no longer running is
taken over, and a live one makes the second run fail.

Symlinks are resolved against the directory that holds them, so a relative
`current` or `previous` target is protected like an absolute one.

### Executors

A running executor keeps its old code until it restarts, so a roll moves the
executors too (task #2354). The script looks for
`unified-agent-executor.service` and `unified-agent-executor-codex.service` on
Linux and for `com.unified-agent.executor-agy` and
`com.unified-agent.executor-codex` on macOS. It handles each one separately,
still holding the roll lock:

| State | What the script does |
|---|---|
| Not installed | Reports it. Not an error. |
| Installed but not running | Leaves it stopped: a restart would start it. |
| Command line names the new release directory | Reports it as already on the release. |
| Has child processes or a live agy tmux session | Reports it as `BUSY`, with those pids or sessions, and does not restart it. |
| Either of those cannot be read | Treated as busy: unknown is not idle. |
| Idle | Restarts it, then verifies it. |

A tmux-backed `agy` run has no child process to find: its private tmux server
detaches from the executor. Each run has a socket at
`/tmp/ua-agy-i-*/tmux.sock` (`UA_ROLL_TMUX_BASE` overrides `/tmp`) and a session
named `ua-agy-i-<executor pid>-<ms>`, so the script asks every such server for
its sessions and counts the ones carrying this executor's pid. A socket whose
server answers `no server running` is a leftover from a killed executor and is
ignored. Any other tmux error, a session name it cannot attribute, an unusable
socket directory, or sockets with no `tmux` on `PATH` all count as busy.

Verification requires a pid different from the old one whose command line
leads to the new release, and the same pid `UA_ROLL_SETTLE_SECONDS` later.
Every `.venv/bin` path on the command line must lead there, so an old
interpreter running a script named through the link fails. A Linux executor
shows both forms: its shebang interpreter names the release directory and its
script argument names `release/current`. If only `release/current` appears, the
script resolves the link and says so. That proves the release only because the
pid is new since the swap, so a link-only command line never counts as already
on the release: the executor is restarted if it is idle.

**Exit status.** `0` means the node and every running executor are on the
release. `3` means the node roll is complete and stays, but at least one
executor is not on the release because it was busy, could not be inspected, or
failed its restart or verification. The warnings name each one. A busy executor
never rolls the node back: the node is already healthy on the new release, and
undoing that would restart it twice to protect a process the roll did not
touch. Non-zero, because a steward must not record an executor as updated when
it is not.

To finish, wait for the executor to go idle and run the same
`--sha <sha>` command again. The node is healthy on that release, so it is left
alone, and only executors still off the release are restarted.

An executor restart ends any run it has in flight, and the executor has no
drain step. The idle check narrows that risk but does not close it: an executor
can claim work between the check and the restart.

**Lazy imports cannot mix releases.** A running executor keeps importing from
the release it started on even after the link moves, because none of its import
paths go through the link. The console script's shebang and the virtualenv's
`.pth` file are absolute paths to the release directory written by `uv sync`,
so `sys.prefix`, `site-packages`, the `src` path and `sys.executable` all name
that directory. Python also resolves `sys.path[0]` to a real path at startup.
The old code stays importable until that worktree is retired, and
`--retire-stale` keeps any worktree named in a running command line.

**The executor must already run from the link.** The roll only restarts
executors; it does not change what they launch. A unit installed verbatim from
`deploy/unified-agent-executor.service` names the checkout, and the Mac plist
templates name `~/dev/ua-current`. On such a host the restart happens but
verification fails with exit `3`, because the new process is not on the new
release. On Linux, a drop-in that sets `ExecStart` to
`%h/.unified_agent/release/current/.venv/bin/unified-agent-executor` fixes it,
in the same way as the node drop-in below.

### launchd constraint (macOS)

`launchctl kickstart` and `bootstrap` are refused inside a supervised Hermes
gateway, which blocks indirect restart loops. Run a Mac roll from a normal
login shell, or have the node's own agent run it. `systemctl --user` on a
remote Linux host is not blocked.

## Roll back

A failed roll rolls itself back. To return to the previous release by hand:

```bash
prev="$(readlink ~/.unified_agent/release/previous)"
ln -sfn "$prev" ~/.unified_agent/release/current.tmp
mv -T ~/.unified_agent/release/current.tmp ~/.unified_agent/release/current   # macOS: mv -h
systemctl --user restart ua-node   # macOS: launchctl kickstart -k gui/$(id -u)/com.unified-agent.node
```

A manual rollback does not move the executors. Restart each idle one the same
way (the two executor units on Linux, the two executor labels on macOS).

Or roll forward to the previous release's commit:
`scripts/node-release-roll.sh --sha <old sha>` reuses the existing worktree.

## Retire stale release worktrees

```bash
scripts/node-release-roll.sh --retire-stale         # list only
scripts/node-release-roll.sh --retire-stale --yes   # git worktree remove, never --force
```

A `release-*` worktree is stale unless it is one of these:

- the target of `current` or `previous`;
- the tree the supervisor's effective program runs, resolved through any link
  it names;
- in a running process's command line.

The command refuses to run when the link is missing or dangling, or when the
supervisor does not name the link. `git worktree remove` refuses a worktree
with changes; it is reported and kept.

Each host has its own link, so retire per host. Move every host to the same
commit first, then run `--check-only` on each host, then `--retire-stale --yes`
on each. A worktree that is stale on one host can be another host's current
target only when the hosts share a filesystem, which the fleet's nodes do not.
