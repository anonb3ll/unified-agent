import { useEffect, useLayoutEffect, useRef, type MouseEvent } from "react";
import { Badge, Button, EmptyState, Field, Input, ErrorState, LinkTabs, PageHeader, Skeleton, UiLink, cx } from "../../lib/ui";
import { describeReadError } from "../lib/queries";
import { useAreaTabs, type AreaTab } from "../lib/navigation";
import { useUrlParams } from "../lib/urlState";
import { isTypingTarget } from "../shell/useAppShortcuts";
import { ItemDetail } from "./ItemDetail";
import { ago, isTab, itemsForTab, KIND_LABEL, TABS, type InboxItem, type InboxTab } from "./model";
import { UNLISTABLE_NOTE, useInbox, type InboxData } from "./useInbox";

export const SECTION_TABS: AreaTab[] = [
  { href: "/inbox", label: "Waiting on you", end: true },
  { href: "/inbox/messages", label: "Messages" },
];

export function itemHref(tab: InboxTab, key: string | null): string {
  const params = new URLSearchParams();
  if (tab !== "all") params.set("kind", tab);
  if (key) params.set("item", key);
  const qs = params.toString();
  return qs ? `/inbox?${qs}` : "/inbox";
}

const EMPTY: Record<InboxTab, string> = {
  all: "Nothing is waiting on you",
  approval: "No approvals are waiting",
  message: "No agent is waiting for your reply",
  hold: "No tasks or holds are waiting",
  proposal: "No proposals are waiting",
  anomaly: "No alerts",
};

function focusDetailHeading(container: HTMLElement | null) {
  let frames = 0;
  const tryFocus = () => {
    const heading = container?.querySelector<HTMLElement>("[data-detail-heading]");
    if (heading) heading.focus();
    else if (frames++ < 60) requestAnimationFrame(tryFocus);
  };
  requestAnimationFrame(tryFocus);
}

function Row({ item, tab, selected, onOpen }: { item: InboxItem; tab: InboxTab; selected: boolean; onOpen: (event: MouseEvent<HTMLAnchorElement>, item: InboxItem) => void }) {
  return (
    <li>
      <UiLink
        href={itemHref(tab, item.key)}
        data-inbox-row={item.key}
        aria-current={selected ? "true" : undefined}
        onClick={(event) => onOpen(event, item)}
        className={cx(
          "flex min-h-11 flex-col gap-0.5 rounded border px-3 py-2 text-sm no-underline text-[var(--ua-fg)] hover:bg-[var(--ua-hover)]",
          "focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-[var(--ua-accent)]",
          selected ? "border-[var(--ua-accent)] bg-[var(--ua-hover)]" : "border-[var(--ua-border)] bg-[var(--ua-surface)]",
        )}
      >
        <span className="flex flex-wrap items-center gap-2 text-xs text-[var(--ua-muted)]">
          <span>{item.kind === "alert" && !item.waiting ? "For your information" : KIND_LABEL[item.kind]}</span>
          {item.since && <span>· {ago(item.since)}</span>}
        </span>
        <span className="break-words font-medium">{item.title}</span>
      </UiLink>
    </li>
  );
}

function ItemList({
  inbox,
  tab,
  selected,
  onOpen,
}: {
  inbox: InboxData;
  tab: InboxTab;
  selected: string;
  onOpen: (event: MouseEvent<HTMLAnchorElement>, item: InboxItem) => void;
}) {
  const rows = itemsForTab(inbox.items, tab);
  const waiting = rows.filter((r) => r.waiting);
  const other = rows.filter((r) => !r.waiting);
  if (inbox.loading) return <Skeleton label="Loading the Inbox" lines={5} />;
  const allFailed = inbox.sources.every((s) => s.query.status === "error");
  if (allFailed) {
    const { message, detail } = describeReadError(inbox.sources[0].query.error);
    return <ErrorState title="The Inbox could not be loaded" message={message} detail={detail} onRetry={inbox.refetchAll} />;
  }
  return (
    <div className="flex flex-col gap-4">
      {waiting.length === 0 ? (
        inbox.countKnown(tab) ? (
          <EmptyState
            headingLevel={2}
            title={EMPTY[tab]}
            description={inbox.unlistable(tab) ? `New items appear here as soon as the hub reports them. ${UNLISTABLE_NOTE}` : "New items appear here as soon as the hub reports them."}
          />
        ) : (
          <p className="m-0 text-sm text-[var(--ua-muted)]">Nothing to show from the parts that could be read. Some sources are unavailable, so this may not be everything.</p>
        )
      ) : (
        <ul aria-label="Items waiting on you" className="m-0 flex list-none flex-col gap-1.5 p-0">
          {waiting.map((item) => (
            <Row key={item.key} item={item} tab={tab} selected={item.key === selected} onOpen={onOpen} />
          ))}
        </ul>
      )}
      {other.length > 0 && (
        <section aria-labelledby="inbox-other-heading" className="flex flex-col gap-1.5">
          <h2 id="inbox-other-heading" className="m-0 text-sm font-semibold text-[var(--ua-muted)]">
            {tab === "approval" ? "Decided, not waiting" : "For your information"}
          </h2>
          <ul aria-label={tab === "approval" ? "Decided approvals" : "Informational alerts"} className="m-0 flex list-none flex-col gap-1.5 p-0">
            {other.map((item) => (
              <Row key={item.key} item={item} tab={tab} selected={item.key === selected} onOpen={onOpen} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

/** Everything waiting on a person, with the action inline (spec §9.1). */
export function InboxPage() {
  const { values, update } = useUrlParams({ kind: "all", item: "", org_author: "", org_scope: "" });
  const tab: InboxTab = isTab(values.kind) ? values.kind : "all";
  const selected = values.item;
  // Org proposals are read at most 200 per state with no paging; author and scope (real filters) reach older ones.
  const inbox = useInbox({ orgFilter: { author: values.org_author, scope: values.org_scope } });
  const orgFiltered = Boolean(values.org_author || values.org_scope);
  const sectionTabs = useAreaTabs(SECTION_TABS);
  const detailRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const openedRef = useRef(false);

  const open = (event: MouseEvent<HTMLAnchorElement>, item: InboxItem) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    openedRef.current = true;
    if (item.key === selected) focusDetailHeading(detailRef.current);
    else update({ item: item.key }, { push: true });
  };

  useEffect(() => {
    if (openedRef.current && selected) {
      openedRef.current = false;
      focusDetailHeading(detailRef.current);
    }
  }, [selected]);

  // j / k move through the list; the selection follows, in the URL. Enter on a row opens it.
  // The listener is bound once and reads the selection from a ref. A router navigation renders as a
  // transition, so a listener re-bound per render could still be the previous one when the next key
  // arrives, and would compare against the selection before the last key (task #2602). The ref
  // takes each key's own selection at once and follows the URL when the selection changes otherwise
  // (back, forward, a click).
  const selectedRef = useRef(selected);
  const updateRef = useRef(update);
  useLayoutEffect(() => {
    selectedRef.current = selected;
  }, [selected]);
  useLayoutEffect(() => {
    updateRef.current = update;
  }, [update]);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "j" && event.key !== "k") return;
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || isTypingTarget(event.target)) return;
      if (document.querySelector("dialog[open]")) return;
      const rows = Array.from(listRef.current?.querySelectorAll<HTMLAnchorElement>("[data-inbox-row]") ?? []);
      if (rows.length === 0) return;
      event.preventDefault();
      const active = rows.findIndex((r) => r === document.activeElement);
      const current = active >= 0 ? active : rows.findIndex((r) => r.dataset.inboxRow === selectedRef.current);
      const next = current < 0 ? 0 : Math.min(rows.length - 1, Math.max(0, current + (event.key === "j" ? 1 : -1)));
      const row = rows[next];
      row.focus();
      row.scrollIntoView?.({ block: "nearest" });
      const key = row.dataset.inboxRow;
      if (key && key !== selectedRef.current) {
        selectedRef.current = key;
        updateRef.current({ item: key });
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const kindTabs = TABS.map((t) => ({
    href: itemHref(t.id, null),
    label: t.label,
    count: inbox.countText(t.id),
    current: t.id === tab,
  }));
  const tabInfo = TABS.find((t) => t.id === tab) ?? TABS[0];

  return (
    <>
      <PageHeader
        title="Inbox"
        description="Everything waiting on a person, with the action next to it. Use j and k to move through the list and Enter to open an item."
        tabs={<LinkTabs label="Inbox sections" items={sectionTabs} />}
      />
      <LinkTabs label="Kinds of item" items={kindTabs} className="mb-2" />
      <p className="m-0 mb-3 text-sm text-[var(--ua-muted)]">{tabInfo.description}</p>
      {inbox.capped(tab) && (
        <p className="m-0 mb-3 rounded border border-[var(--ua-warn)] px-3 py-2 text-sm">
          More items may be waiting than are shown.{" "}
          {(tab === "all" || tab === "approval") && "The hub returns at most 100 waiting approvals at a time; deciding some brings the rest into view. "}
          {(tab === "all" || tab === "proposal") &&
            "Only the newest 200 organization proposals in each state can be read here: the hub has no paging for them. Narrow them by author or scope to reach older ones. "}
          {(tab === "all" || tab === "message" || tab === "hold") && "The hub lists at most 100 of each kind of task, reply or wake request. "}
        </p>
      )}
      {tab === "proposal" && (inbox.capped("proposal") || orgFiltered) && (
        <div role="group" aria-label="Narrow organization proposals" className="mb-3 grid gap-2 sm:grid-cols-2">
          <Field label="Proposals by author" hint="An agent label, exactly as the hub has it.">
            <Input value={values.org_author} onChange={(e) => update({ org_author: e.target.value })} />
          </Field>
          <Field label="Proposals for scope" hint="The part of the organization the proposal changes.">
            <Input value={values.org_scope} onChange={(e) => update({ org_scope: e.target.value })} />
          </Field>
        </div>
      )}
      {tab === "proposal" && (
        <p className="m-0 mb-3 rounded border border-[var(--ua-border-strong)] px-3 py-2 text-sm">
          Work proposals cannot be listed on this hub yet, so they are not shown here. Agents that file one link to it
          when they message you.
        </p>
      )}
      {inbox.failed.length > 0 && !inbox.sources.every((s) => s.query.status === "error") && (
        <section aria-labelledby="inbox-partial-heading" className="mb-4 flex flex-col gap-2 rounded-lg border border-[var(--ua-warn)] px-4 py-3">
          <h2 id="inbox-partial-heading" className="m-0 text-sm font-semibold">
            Part of the Inbox could not be read
          </h2>
          <p className="m-0 text-sm">
            What could be read is shown. Counts that depend on the missing parts show “–” until they load.
          </p>
          <ul className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
            {inbox.failed.map((f) => {
              const { message, detail } = describeReadError(f.error);
              const notOffered = /HTTP 404/.test(String(detail));
              return (
                <li key={f.label} className="flex flex-wrap items-center gap-2">
                  <Badge variant="warning">{f.label}</Badge>
                  <span>{notOffered ? "This hub does not offer this yet." : message}</span>
                  <Button size="sm" onClick={f.retry} aria-label={`Retry ${f.label}`}>
                    Retry
                  </Button>
                  {detail && (
                    <details className="text-xs">
                      <summary className="cursor-pointer">Technical detail</summary>
                      <pre className="m-0 mt-1 whitespace-pre-wrap break-words font-mono">{detail}</pre>
                    </details>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      )}
      <div className="grid gap-5 lg:grid-cols-[minmax(18rem,26rem)_1fr]">
        <div ref={listRef} className={cx(selected && "max-lg:hidden")}>
          <ItemList inbox={inbox} tab={tab} selected={selected} onOpen={open} />
        </div>
        <div ref={detailRef} role="region" aria-label="Selected item" className={cx("min-w-0", !selected && "max-lg:hidden")}>
          {selected ? (
            <div className="flex flex-col gap-3">
              <UiLink href={itemHref(tab, null)} className="text-sm underline lg:hidden">
                Back to the list
              </UiLink>
              <ItemDetail itemKey={selected} inbox={inbox} />
            </div>
          ) : (
            <EmptyState headingLevel={2} title="Pick an item" description="Its details and the action it needs show here." />
          )}
        </div>
      </div>
    </>
  );
}
