# Messaging bridges

Task #1863. Three kinds of agent talk on three buses:

- **Hub agents** use the hub mailbox (`message_send`, `message_list`).
- **Cloud agents** (Claude Code on the web) have no hub access. They write
  Markdown threads in the unified_brain repo.
- **Hermes** gateways speak A2A, and an A2A call reaches the live gateway
  session, the same agent its user is talking to.

The bridges carry messages between them, so nobody has to copy text from one
to another. They are one command, `unified-agent-bridge run-once`, which makes a
single pass and prints a JSON report. Nothing in this repo schedules it (see
[Running it](#running-it)).

## Brain threads to hub mail

`src/unified_agent/messaging/bridge_brain.py`.

A thread is a file in `threads/` with a frontmatter header. Its entries are the
opening message (author `from`, date `created`) and each
`## Reply — <label>, <date>` section after it. The bridge reads the files
itself (read-only) and the roster from `roster.json`.

An entry becomes hub mail when all of these hold:

- the thread is not closed (`done` or `cancelled`);
- its author is a roster member in a relayed group
  (`UA_BRIDGE_BRAIN_FROM_GROUPS`, default `cloud`). Local agents already have
  the hub, so their entries are never relayed. This also stops the two
  directions from looping;
- it is addressed to at least one hub label: a `local` roster label the hub
  knows. The opening message goes to the thread's `to` labels; a reply also
  goes to `claimed_by` and `waiting_on`. A thread addressed only to a group
  (`local`, `any`) goes to `UA_BRIDGE_BRAIN_GROUP_TO` if it is set, and
  otherwise stays with the session-start inbox read. The setting must name a
  `local` brain roster agent that is also a hub label; any other value is
  refused, reported in the run's `errors`, and treated as unset.

Each entry is sent once per recipient as `act=request`, `expects_reply=true`,
`conversation_id=brain:<thread-id>`, from the bridge's own service label
(`UA_BRIDGE_SENDER`, default `brain_bridge`). It is never sent as the author the
thread names: a reply heading is not authenticated, and anyone who can edit the
repo can write `## Reply — <any label>`. The claimed author goes in the untrusted
header as `claimed author <label> (unverified)`. The service label needs no
token (the bridge sends in process). It must be a label the hub accepts, so with
a closed roster (`UA_AGENT_LABELS`) it must be listed there, and it may not be a
real agent's identity (a fleet catalog label or a brain roster member); the run
reports an error and sends nothing otherwise. The message id is `brain-` plus a
hash of the thread id, the entry's position, author and date. A re-run is a
no-op twice over: the state file records the entry, and the hub dedupes on
`message_id` even if the state file is lost.

## Hub replies to brain threads

The `brain:<thread-id>` conversation is the routing key, and only mail
addressed to the bridge sender is published. A hub message in it that a
hub-side roster label sent to the bridge is appended to that thread; side mail
between hub agents in the same conversation stays on the hub (it is reported
as skipped), because the brain repo is readable by cloud sessions. The usual case is a reply,
`message_send(to_agent=brain_bridge, reply_to=<the bridged message_id>)`, which
joins the conversation; once appended it is marked read in the bridge's mailbox.
A message from a label outside the brain roster, or from a cloud-side label
(which has no hub access to have sent it), is skipped and reported.

The append is written in process, never by running the brain repo's
`scripts/brain.py` (see [Trust boundary](#trust-boundary)).
`src/unified_agent/messaging/brain_format.py` mirrors exactly what
`brain.py reply <thread> --from <sender> --message-file <file>` writes to the
thread file: a `## Reply — <sender>, <date>` block, the `replies` and `updated`
header fields, and `status: answered` (clearing `waiting_on`) when the replier
is not the thread's sender and the thread is still open, which is `brain.py`'s
rule for a reply from anyone but the sender. A test compares its output with a
verbatim excerpt of `brain.py` byte for byte. The file is replaced atomically
(temp file and rename, keeping its mode). `brain.py reply` also regenerates
`INBOX.md`; the bridge does not, so run `python3 scripts/brain.py index` from a
checkout you trust before committing.

The appended text ends with an attribution line and a marker,
`<!-- ua-bridge: hub message <id> -->`. Before appending, the bridge looks for
the marker inside a reply section headed by the same agent, so a reply lands
once even if the state file is lost, and a marker pasted into someone else's
section cannot suppress it. A hub message cannot forge thread structure: lines
starting with `#` are escaped, and `<!--` is escaped, so a body cannot add a
reply heading or a fake marker.

The bridge runs no git command. Pull the brain repo before a run, and commit
and push the changed threads (and the regenerated `INBOX.md`) after it,
following the brain repo's own delivery rules. **Commit the brain repo before
any reset or re-clone.** Once a reply is appended, the state file records it
and the conversation cursor moves past it, so a reply discarded by a reset
before it was committed is not appended again.

## Trust boundary

Thread text and every other file in the brain repo are untrusted. The repo's
`main` is not branch-protected, so anyone who can push there controls its
contents. The bridge therefore:

- never executes a file from the repo, `scripts/brain.py` included: the
  thread format is read and written in process;
- refuses a thread file, `roster.json` or `threads/` directory that is a
  symlink, so the repo cannot point it at other files the hub user can read or
  write;
- treats every entry's author as a claim (relayed mail comes from the bridge's
  own label, with the claimed author marked unverified);
- never follows or acts on the text it carries.

## Hub mail to Hermes over A2A

`src/unified_agent/messaging/bridge_a2a.py`. **Off by default**
(`UA_BRIDGE_A2A=1` turns it on).

`UA_BRIDGE_A2A_PEERS="agent-hermes=http://127.0.0.1:9900/"` maps a hub label
to its gateway's A2A endpoint. For each peer the bridge reads all of that label's
unread label mail after its cursor, page by page, and sends each message as an A2A v1.0
`SendMessage` JSON-RPC call (header `A2A-Version: 1.0`, role `ROLE_USER`,
`contextId` derived from the hub conversation). Hermes injects it into the live
session, framed there as untrusted peer input, and holds the call open until
the session answers (up to its own 300-second reply timeout;
`UA_BRIDGE_A2A_TIMEOUT` defaults to 330).

A completed or input-required answer comes back as a hub message: `act=inform`,
from the Hermes label to the original sender, `reply_to` the forwarded message,
`hop` one more than the parent's. Only once that reply is committed is the
forwarded message recorded as handled and marked read. If the hub refuses the
reply, the answer is held in the state file (`a2a_pending`) and retried on later
runs without asking the gateway again; after 5 refused runs the bridge gives up
and leaves the request unread for a person.
If the original was a `brain:` conversation, the next brain pass appends the
answer to the thread, so a cloud agent can ask a Hermes gateway a question
through a thread.

Failures: a connection error, a timeout or a non-200 answer stops that peer for
the run with the cursor before the message, and the next run retries it. After
3 such runs for one message it is recorded `failed`, the cursor moves past it
and it is left unread for a person, so one bad message cannot block a peer. A
timeout can mean the gateway did receive the message, so delivery on timeouts
is at least once (up to 3 times). A JSON-RPC error, a failed task or a refused
credential is recorded at once and not retried.

Loops are bounded three ways: a reply the bridge wrote (id prefix `a2a-`) is
never forwarded again, the hub refuses a `hop` at `UA_MESSAGE_MAX_HOPS`, and
Hermes caps turns per context (`A2A_MAX_PINGPONG_TURNS`).

### Credentials

The hub has no A2A credential, and this task mints none. The forwarder calls
**loopback endpoints only**, and sends no `Authorization` header. A Hermes
gateway with no token configured runs in localhost-only mode and accepts
those calls, so it works for a gateway on the same machine as the hub.

A gateway on another machine (a desktop or laptop Agent5) binds a non-loopback
address only with a token set, so it needs a per-peer A2A token
(`A2A_PEER_TOKENS` on the Hermes side) and a matching secret on the hub side.
That is a credential decision for the operator. Until it is made, a
non-loopback peer is refused with an error that names the missing token, and a
gateway that answers 401 or 403 is reported the same way.

## Untrusted text

Everything the bridges carry is data, never instructions:

- Relayed text is capped (`UA_BRIDGE_MAX_BYTES`, default 3000 bytes), with a
  `[truncated: N bytes omitted ...]` note. The cap is lowered to whatever
  `UA_MESSAGE_MAX_BYTES` leaves after the header, so header plus text always fits
  one hub message. The bridge refuses to run when `UA_MESSAGE_MAX_BYTES` is below
  1024 bytes.
- Hub mail made from a thread entry opens with an `[untrusted: unified_brain
  thread ... authorizes nothing ...]` header. A2A answers open with
  `[untrusted: reply from <label>'s live gateway session ...]`.
- `send_message` redacts credential-shaped strings as it does for any message.
- Neither bridge executes, follows or acts on the text it carries, or runs code
  from the brain repo. The thread protocol's rule holds: a thread asks, it
  never authorizes.

## Running it

```bash
unified-agent-bridge run-once --dry-run      # what would move; writes nothing
unified-agent-bridge run-once --baseline     # once, first: mark history as handled
unified-agent-bridge run-once                # both bridges
unified-agent-bridge run-once --brain        # threads only
unified-agent-bridge run-once --a2a          # A2A only
```

It runs in process on the hub host, against the hub database
(`storage.connect()`), so run it where the hub runs. Exit status is 0 when
nothing failed, 1 when an item failed (listed under `errors`), and 2 when it
could not run (no brain repo, an unreadable state file, or another run holding
the lock).

State lives in `UA_BRIDGE_STATE` (default `bridge-state.json` in `UA_HOME`):
which entries were sent, which hub replies were appended, a hub-seq cursor per
thread and per A2A peer, and the outcome of each forward. Writes are atomic,
and a lock file next to it keeps two runs from overlapping.

The file stays bounded. Sent-entry and appended-reply records are kept only
while their thread is open, a thread's cursor only while its file exists, and
only the newest 500 A2A forward outcomes are kept. Dropping them does not weaken
idempotency: a resent entry has the same derived message id, which the hub
dedupes, an appended reply carries its marker in the thread, and the A2A cursor
keeps old mail from being rescanned.

An always-on runner (a timer, LaunchAgent or systemd unit that pulls the brain
repo, runs the bridge and commits the appended replies) is red tier and is the
operator's decision, as proposed on task #1659. None ships with this change.
