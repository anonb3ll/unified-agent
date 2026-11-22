/** EventSource transport: refusal backoff and cursor recovery. */
export const EVENTS_STREAM_URL = "/api/events/stream";
export const POLL_BACKOFF_MAX_MS = 5 * 60_000;

/**
 * First wait before the Control Center re-opens an event stream the hub refused.
 * Matches the hub's `Retry-After` for a full stream cap (30 s).
 */
export const STREAM_RECONNECT_MS = 30_000;
// `EventSource.CLOSED`; a literal so a test double without the static still works.
const EVENT_SOURCE_CLOSED = 2;

/**
 * The hub answers a stream it cannot admit (cap full) with HTTP 429 and a
 * `Retry-After`. A browser `EventSource` cannot read either: any non-200
 * response fails the connection for good, with `readyState === CLOSED` and no
 * automatic retry, so `retry:` hints never apply. This helper does the retry
 * itself: it re-opens a CLOSED stream after {@link STREAM_RECONNECT_MS},
 * doubling per consecutive refusal up to {@link POLL_BACKOFF_MAX_MS} and
 * resetting on a successful `open`, so a full hub is never hot-looped. An
 * ordinary network drop leaves the stream CONNECTING and the browser retries it
 * (sending `Last-Event-ID`); only the CLOSED case is handled here.
 *
 * A re-opened stream cannot send `Last-Event-ID`, so the last id seen is passed
 * as `since` (an event ULID) or `after_seq` (`seq:<n>`) to resume without a gap.
 *
 * `attach` runs for every `EventSource` created and adds the caller's listeners.
 * Returns a function that stops everything and closes the current stream.
 */
export function openEventStream(url: string, attach: (stream: EventSource) => void): () => void {
  let stream: EventSource | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let stopped = false;
  let refusals = 0;
  let lastId = "";

  const resumeUrl = (): string => {
    if (!lastId) return url;
    const param = lastId.startsWith("seq:")
      ? `after_seq=${encodeURIComponent(lastId.slice(4))}`
      : `since=${encodeURIComponent(lastId)}`;
    // Replace a cursor the caller's URL already carries rather than adding a
    // second one: the hub refuses both `since` and `after_seq` with a 400.
    const [path, query = ""] = url.split("?", 2);
    const kept = query
      .split("&")
      .filter((pair) => pair !== "" && !/^(since|after_seq)(=|$)/.test(pair));
    return `${path}?${[...kept, param].join("&")}`;
  };

  const connect = () => {
    const current = new EventSource(lastId ? resumeUrl() : url);
    stream = current;
    const remember = (event: Event) => {
      const id = (event as MessageEvent).lastEventId;
      if (id) lastId = id;
    };
    current.addEventListener("open", () => {
      refusals = 0;
    });
    current.addEventListener("cursor", remember);
    current.addEventListener("message", remember);
    current.addEventListener("error", () => {
      if (stopped || current.readyState !== EVENT_SOURCE_CLOSED) return;
      current.close();
      refusals += 1;
      const delay = Math.min(STREAM_RECONNECT_MS * 2 ** (refusals - 1), POLL_BACKOFF_MAX_MS);
      timer = setTimeout(connect, delay);
    });
    attach(current);
  };

  connect();
  return () => {
    stopped = true;
    if (timer !== undefined) clearTimeout(timer);
    stream?.close();
  };
}
