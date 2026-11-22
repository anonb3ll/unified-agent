"""Read access to the swarm event log (task #157).

Deliberately read-only. Publishing stays in-process (``events.record_event``
and the ``storage.record_agent_event`` mirror) rather than being exposed as a
tool: task #156's policy engine is designed to fail CLOSED on a missing episode
record, and a tool that let any caller mint arbitrary episode and causal-parent
ids would turn that guarantee into a formality. Same reasoning as the socket
not being an authentication boundary — a ledger is only worth what the code
writing it is worth.
"""

from mcp.server.mcpserver.exceptions import ToolError

from .. import edition, events


def event_log_query(
    channels: list[str] | None = None,
    since: str | None = None,
    limit: int = 100,
    after_seq: int | None = None,
    item_id: str | None = None,
    channel_prefix: str | None = None,
) -> dict:
    """Read the append-only swarm event log (task #157), oldest first.

    The event log is the ordered record of notable cluster mutations — task
    claims and updates, approval requests, delegations, memory saves, journal
    entries — keyed by ULID so a consumer can resume exactly where it left off.

    `channels` filters by channel name; a trailing `*` matches a prefix
    (`agent.task.*`), a bare `*` matches everything, anything else is exact.
    `channel_prefix` is the same prefix filter as one string (`agent.approval`
    matches `agent.approval` and `agent.approval.requested`). Clients send
    that name; it is not a separate query language. Passing both unions them.
    Known channels: `agent.task.updated`, `agent.approval.requested`,
    `agent.health.alert`, `agent.supervisor.wake` (FirstMate-style COS wakes on
    completion/block/needs-decision — task #1028), plus `agent.<kind>.updated`
    for other mutation kinds.

    `since` is a ULID cursor and is EXCLUSIVE: pass back the `cursor` from a
    previous call to get only what has happened since, with no re-processing
    and no gap. `limit` caps the rows returned (default 100).

    `after_seq` is the gap-free alternative (task #1640): every event carries
    `seq`, its commit position, and rows come back in that order. A `since`
    consumer can miss a row that committed behind a newer ULID; an
    `after_seq` consumer cannot. Pass one cursor or the other, not both.

    `item_id` keeps only events whose payload's top-level `item_id` equals it,
    e.g. one node dispatch item's `agent.node` events, so a session transcript
    reloads its own turns however many fleet events came after (task #1992).
    An item read past its first page (`after_seq` > 0 or `since`) omits the
    source fields: they count the whole channel, and a transcript that pages
    or refreshes one item must not recount it every time.

    Returns {events: [{seq, ulid, channel, payload, originator, episode_id,
    causal_parent_id, created_at}], count, cursor, seq_cursor,
    source_event_count, last_event_at}. The source fields ignore the cursor:
    an empty result with a nonzero source count is an idle window, while
    zero/None means no matching event writer has ever been observed. `cursor`
    is the last ULID returned and `seq_cursor` the last seq (None when nothing
    matched) — feed them back as `since` / `after_seq`.
    """
    if since and after_seq is not None:
        raise ToolError("pass either since (ULID) or after_seq, not both")
    if channel_prefix is not None and not isinstance(channel_prefix, str):
        raise ToolError("channel_prefix must be a string")
    selected = events.merge_channel_prefix(channels, channel_prefix)
    rows = events.query_events(
        channels=selected, since=since, limit=limit, after_seq=after_seq, item_id=item_id
    )
    result = {
        "events": rows,
        "count": len(rows),
        "cursor": rows[-1]["ulid"] if rows else None,
        "seq_cursor": rows[-1]["seq"] if rows else None,
    }
    if not (item_id and (since or (after_seq or 0) > 0)):
        result.update(events.event_log_liveness(channels=selected))
    return result


def event_log_verify() -> dict:
    """Verify the tamper-evident hash chain of the event/audit log (task #746).

    Recomputes SHA-256 hashes across all chained events to ensure no entries
    have been altered, deleted, or inserted out of order.

    Returns verification report including status ('valid'|'tampered'|'empty'),
    total_verified count, head_hash, and tampered_at details if corruption is found.
    An unhashed row after the chain started is reported as tampered (reason
    'unhashed_row_after_chain_start', offenders in unhashed_rows); unhashed rows
    before it are a legacy prefix, counted in unhashed_skipped (task #1908).
    """
    edition.require_feature("audit_hash_chain")
    return events.verify_event_chain()
