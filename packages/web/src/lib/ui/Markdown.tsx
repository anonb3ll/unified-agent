import type { ReactNode } from "react";
import { cx } from "./cx";

/**
 * A small Markdown renderer that builds React elements and never injects
 * HTML: raw `<tags>` in the source render as text, link targets are limited
 * to http(s), mailto and relative URLs, and images render as links (no
 * third-party fetch from a note). Supported: ATX headings, paragraphs,
 * emphasis, strong, strikethrough, inline code, fenced code, block quotes,
 * ordered/unordered/task lists (nested by indent), tables, rules, links,
 * autolinks and `[[wikilinks]]`.
 */
export type MarkdownProps = {
  source: string;
  /** Added to every heading level, so a note's `#` does not become a second page `h1`. Default 1. */
  headingOffset?: number;
  /** Render `[[target|label]]`. Default: the label as plain text. */
  renderWikilink?: (target: string, label: string) => ReactNode;
  /** YAML front matter at the top: hidden (default) or shown as a code block. */
  frontmatter?: "hide" | "show";
  className?: string;
};

const SCHEME = /^([a-z][a-z0-9+.-]*):/i;

/** The URL if it is safe to put in an href, else null. */
export function safeHref(raw: string): string | null {
  // Browsers ignore control characters and whitespace inside a scheme ("java\tscript:").
  const url = raw.trim().replace(/[\u0000- \u007f]/g, "");
  if (!url) return null;
  const scheme = SCHEME.exec(url);
  if (scheme) {
    return ["http", "https", "mailto"].includes(scheme[1].toLowerCase()) ? url : null;
  }
  return url;
}

type Ctx = Required<Pick<MarkdownProps, "headingOffset">> & Pick<MarkdownProps, "renderWikilink">;

// ---------------------------------------------------------------- inline

const INLINE: Array<{ re: RegExp; render: (m: RegExpExecArray, key: string, ctx: Ctx) => ReactNode }> = [
  {
    re: /`([^`]+)`/y,
    render: (m, key) => (
      <code key={key} className="rounded bg-[var(--ua-sunken)] px-1 py-0.5 font-mono text-[0.9em]">
        {m[1]}
      </code>
    ),
  },
  {
    re: /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/y,
    render: (m, key, ctx) => {
      const target = m[1].trim();
      const label = (m[2] ?? m[1]).trim();
      return <span key={key}>{ctx.renderWikilink ? ctx.renderWikilink(target, label) : label}</span>;
    },
  },
  {
    re: /!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/y,
    render: (m, key, ctx) => renderLink(key, m[2], m[1] ? `Image: ${m[1]}` : "Image", ctx, false),
  },
  {
    re: /\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/y,
    render: (m, key, ctx) => renderLink(key, m[2], m[1], ctx, true),
  },
  {
    re: /<((?:https?:\/\/|mailto:)[^>\s]+)>/y,
    render: (m, key, ctx) => renderLink(key, m[1], m[1], ctx, false),
  },
  {
    re: /(\*\*|__)(?=\S)([\s\S]*?\S)\1/y,
    render: (m, key, ctx) => <strong key={key}>{inline(m[2], ctx, key)}</strong>,
  },
  {
    re: /~~(?=\S)([\s\S]*?\S)~~/y,
    render: (m, key, ctx) => <del key={key}>{inline(m[1], ctx, key)}</del>,
  },
  {
    re: /(\*|_)(?=\S)([\s\S]*?\S)\1(?![*_\w])/y,
    render: (m, key, ctx) => <em key={key}>{inline(m[2], ctx, key)}</em>,
  },
];

function renderLink(key: string, href: string, text: string, ctx: Ctx, parseText: boolean): ReactNode {
  const safe = safeHref(href);
  const children = parseText ? inline(text, ctx, key) : text;
  if (!safe) return <span key={key}>{children}</span>;
  const external = /^(https?:|mailto:)/i.test(safe);
  return (
    <a
      key={key}
      href={safe}
      className="text-[var(--ua-link)] underline"
      {...(external ? { rel: "noopener noreferrer nofollow", target: "_blank" } : {})}
    >
      {children}
      {external && <span className="sr-only"> (opens in a new tab)</span>}
    </a>
  );
}

function inline(text: string, ctx: Ctx, keyPrefix = "i"): ReactNode[] {
  const out: ReactNode[] = [];
  let plain = "";
  let i = 0;
  const flush = () => {
    if (plain) out.push(plain);
    plain = "";
  };
  while (i < text.length) {
    const ch = text[i];
    if (ch === "\\" && i + 1 < text.length && /[\\`*_{}[\]()#+\-.!|~<>]/.test(text[i + 1])) {
      plain += text[i + 1];
      i += 2;
      continue;
    }
    let matched = false;
    if ("`[!<*_~".includes(ch)) {
      for (const rule of INLINE) {
        rule.re.lastIndex = i;
        const m = rule.re.exec(text);
        if (m) {
          flush();
          out.push(rule.render(m, `${keyPrefix}.${out.length}`, ctx));
          i += m[0].length;
          matched = true;
          break;
        }
      }
    }
    if (!matched) {
      plain += ch;
      i += 1;
    }
  }
  flush();
  return out;
}

// ---------------------------------------------------------------- blocks

const FENCE = /^\s{0,3}(`{3,}|~{3,})\s*([\w+-]*)/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$/;
const QUOTE = /^\s{0,3}>\s?/;
const LIST = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;

function splitRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}

function startsBlock(line: string, next: string | undefined): boolean {
  return (
    FENCE.test(line) ||
    HEADING.test(line) ||
    RULE.test(line) ||
    QUOTE.test(line) ||
    LIST.test(line) ||
    (line.includes("|") && next !== undefined && TABLE_SEP.test(next))
  );
}

function headingTag(level: number) {
  return `h${Math.min(6, Math.max(1, level))}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
}

const HEADING_CLASS: Record<string, string> = {
  h1: "text-xl",
  h2: "text-lg",
  h3: "text-base",
  h4: "text-sm",
  h5: "text-sm",
  h6: "text-sm",
};

function parseList(lines: string[], ctx: Ctx, key: string): ReactNode {
  const first = LIST.exec(lines[0]);
  if (!first) return null;
  const indent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const items: { text: string; children: string[] }[] = [];
  for (const line of lines) {
    const m = LIST.exec(line);
    if (m && m[1].length <= indent) {
      items.push({ text: m[3], children: [] });
    } else if (items.length > 0) {
      const last = items[items.length - 1];
      if (m || line.trim() === "") last.children.push(line);
      else if (last.children.length > 0) last.children.push(line);
      else last.text += ` ${line.trim()}`;
    }
  }
  const Tag = ordered ? "ol" : "ul";
  const start = ordered ? Number.parseInt(first[2], 10) : undefined;
  return (
    <Tag key={key} start={start !== 1 ? start : undefined} className={cx("my-2 pl-6", ordered ? "list-decimal" : "list-disc")}>
      {items.map((item, index) => {
        const task = /^\[([ xX])\]\s+(.*)$/.exec(item.text);
        const nested = item.children.filter((l) => l.trim() !== "");
        return (
          <li key={index} className={cx("my-0.5", task && "list-none -ml-5")}>
            {task ? (
              <>
                <span aria-hidden="true" className="mr-1.5 font-mono">
                  {task[1] === " " ? "☐" : "☑"}
                </span>
                <span className="sr-only">{task[1] === " " ? "To do: " : "Done: "}</span>
                {inline(task[2], ctx, `${key}.${index}`)}
              </>
            ) : (
              inline(item.text, ctx, `${key}.${index}`)
            )}
            {nested.length > 0 && LIST.test(nested[0]) && parseList(nested, ctx, `${key}.${index}.l`)}
          </li>
        );
      })}
    </Tag>
  );
}

function parseBlocks(lines: string[], ctx: Ctx, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const key = `${keyPrefix}${out.length}`;
    if (line.trim() === "") {
      i += 1;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence) {
      const marker = fence[1];
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(marker)) body.push(lines[i++]);
      i += 1;
      out.push(
        <pre
          key={key}
          tabIndex={0}
          aria-label={fence[2] ? `Code (${fence[2]})` : "Code"}
          className="my-3 overflow-x-auto rounded-md border border-[var(--ua-border)] bg-[var(--ua-sunken)] p-3 font-mono text-xs leading-relaxed focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)]"
        >
          <code>{body.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      const Tag = headingTag(heading[1].length + ctx.headingOffset);
      out.push(
        <Tag key={key} className={cx("mb-2 mt-4 font-semibold first:mt-0", HEADING_CLASS[Tag])}>
          {inline(heading[2], ctx, key)}
        </Tag>,
      );
      i += 1;
      continue;
    }
    if (RULE.test(line)) {
      out.push(<hr key={key} className="my-4 border-0 border-t border-[var(--ua-border)]" />);
      i += 1;
      continue;
    }
    if (QUOTE.test(line)) {
      const body: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i])) body.push(lines[i++].replace(QUOTE, ""));
      out.push(
        <blockquote key={key} className="my-3 border-0 border-l-4 border-[var(--ua-border-strong)] pl-3 text-[var(--ua-muted)]">
          {parseBlocks(body, ctx, `${key}.`)}
        </blockquote>,
      );
      continue;
    }
    if (LIST.test(line)) {
      const head = LIST.exec(line)!;
      const baseIndent = head[1].length;
      const ordered = /\d/.test(head[2]);
      // A top-level marker of the other kind (bullet vs number) starts a new list.
      const otherKind = (candidate: string) => {
        const m = LIST.exec(candidate);
        return Boolean(m && m[1].length <= baseIndent && /\d/.test(m[2]) !== ordered);
      };
      const body: string[] = [];
      while (i < lines.length) {
        const current = lines[i];
        if (body.length > 0 && otherKind(current)) break;
        if (current.trim() === "") {
          const next = lines[i + 1];
          if (next !== undefined && !otherKind(next) && (LIST.test(next) || /^\s{2,}\S/.test(next))) {
            body.push(current);
            i += 1;
            continue;
          }
          break;
        }
        if (!LIST.test(current) && !/^\s+\S/.test(current) && body.length > 0 && startsBlock(current, lines[i + 1])) break;
        body.push(current);
        i += 1;
      }
      out.push(parseList(body, ctx, key));
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const head = splitRow(line);
      const align = splitRow(lines[i + 1]).map((c) =>
        c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : "left",
      );
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") rows.push(splitRow(lines[i++]));
      out.push(
        <div key={key} tabIndex={0} role="region" aria-label="Table" className="my-3 overflow-x-auto focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)]">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr>
                {head.map((cell, c) => (
                  <th key={c} scope="col" style={{ textAlign: align[c] as "left" }} className="border-b border-[var(--ua-border-strong)] px-2 py-1 font-semibold">
                    {inline(cell, ctx, `${key}.h${c}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>
                  {head.map((_, c) => (
                    <td key={c} style={{ textAlign: align[c] as "left" }} className="border-b border-[var(--ua-border)] px-2 py-1">
                      {inline(row[c] ?? "", ctx, `${key}.${r}.${c}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() !== "" && (para.length === 0 || !startsBlock(lines[i], lines[i + 1]))) {
      para.push(lines[i++]);
    }
    out.push(
      <p key={key} className="my-2">
        {para.flatMap((p, n) => {
          const hardBreak = / {2,}$/.test(p) && n < para.length - 1;
          const parts = inline(n < para.length - 1 && !hardBreak ? `${p.trim()} ` : p.trim(), ctx, `${key}.${n}`);
          return hardBreak ? [...parts, <br key={`${key}.${n}.br`} />] : parts;
        })}
      </p>,
    );
  }
  return out;
}

export function Markdown({ source, headingOffset = 1, renderWikilink, frontmatter = "hide", className }: MarkdownProps) {
  let text = source.replace(/\r\n?/g, "\n");
  let front: string | null = null;
  const fm = /^---\n([\s\S]*?)\n---(?:\n|$)/.exec(text);
  if (fm) {
    front = fm[1];
    text = text.slice(fm[0].length);
  }
  const ctx: Ctx = { headingOffset, renderWikilink };
  return (
    <div className={cx("text-sm leading-relaxed text-[var(--ua-fg)] break-words", className)}>
      {front !== null && frontmatter === "show" && (
        <pre
          tabIndex={0}
          aria-label="Front matter"
          className="mb-3 overflow-x-auto rounded-md border border-[var(--ua-border)] bg-[var(--ua-sunken)] p-3 font-mono text-xs"
        >
          <code>{front}</code>
        </pre>
      )}
      {parseBlocks(text.split("\n"), ctx, "b")}
    </div>
  );
}
