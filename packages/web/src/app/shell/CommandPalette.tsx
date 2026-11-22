import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate } from "react-router";
import { cx, Dialog, Input } from "../../lib/ui";
import { useTheme } from "../../lib/theme/ThemeProvider";
import { commandScore, matchesCommand, type PaletteCommand } from "../lib/commands";
import { loadAreaCommands } from "../router";
import { CREATE_ACTIONS, DESTINATIONS, ROUTE_SAMPLES } from "../routes";

function baseCommands(onOpenHelp: () => void, setTheme: ReturnType<typeof useTheme>["setTheme"]): PaletteCommand[] {
  const goTo: PaletteCommand[] = DESTINATIONS.map((d) => ({
    id: `go-${d.id}`,
    label: d.label,
    group: "Go to",
    to: d.path,
    shortcut: `g ${d.shortcut}`,
    keywords: [d.description],
  }));
  const create: PaletteCommand[] = CREATE_ACTIONS.map((a) => ({
    id: `new-${a.id}`,
    label: `New ${a.label.toLowerCase()}`,
    group: "Create",
    to: a.path,
  }));
  const destinationPaths = new Set(DESTINATIONS.map((d) => d.path));
  const pages: PaletteCommand[] = ROUTE_SAMPLES.filter((r) => !/\d|Desktop_|abc/.test(r.path) && !destinationPaths.has(r.path)).map((r) => ({
    id: `page-${r.path}`,
    label: r.title,
    group: "Pages",
    to: r.path,
  }));
  const app: PaletteCommand[] = [
    { id: "help", label: "Keyboard shortcuts", group: "App", shortcut: "?", run: onOpenHelp },
    { id: "theme-light", label: "Use light theme", group: "App", run: () => setTheme("light") },
    { id: "theme-dark", label: "Use dark theme", group: "App", run: () => setTheme("dark") },
    { id: "theme-hc", label: "Use high contrast theme", group: "App", run: () => setTheme("high-contrast") },
  ];
  return [...goTo, ...create, ...pages, ...app];
}

/**
 * ⌘K / Ctrl+K / "/": find a page or run a command. A modal dialog holding an
 * ARIA 1.2 combobox: type to filter, Up/Down to move, Enter to go.
 */
export function CommandPalette({ open, onClose, onOpenHelp }: { open: boolean; onClose: () => void; onOpenHelp: () => void }) {
  const navigate = useNavigate();
  const { setTheme } = useTheme();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [areaCommands, setAreaCommands] = useState<PaletteCommand[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const listId = `${useId()}-list`;

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
    let live = true;
    void loadAreaCommands().then((commands) => {
      if (live) setAreaCommands(commands);
    });
    return () => {
      live = false;
    };
  }, [open]);

  const all = useMemo(() => {
    const base = baseCommands(onOpenHelp, setTheme);
    const seen = new Set(base.map((c) => c.id));
    return [...base, ...areaCommands.filter((c) => !seen.has(c.id))];
  }, [areaCommands, onOpenHelp, setTheme]);
  const visible = useMemo(
    () =>
      all
        .filter((c) => matchesCommand(c, query))
        .map((c, order) => ({ c, order, score: commandScore(c, query) }))
        .sort((a, b) => b.score - a.score || a.order - b.order)
        .map((x) => x.c)
        .slice(0, 50),
    [all, query],
  );
  const index = Math.min(active, Math.max(visible.length - 1, 0));

  const choose = (command: PaletteCommand | undefined) => {
    if (!command) return;
    onClose();
    if (command.to) navigate(command.to);
    command.run?.({ navigate });
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((index + 1) % Math.max(visible.length, 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index - 1 + visible.length) % Math.max(visible.length, 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(visible[index]);
    }
  };

  const optionId = (i: number) => `${listId}-${i}`;
  useEffect(() => {
    document.getElementById(optionId(index))?.scrollIntoView?.({ block: "nearest" });
  }, [index]);

  return (
    <Dialog open={open} onClose={onClose} title="Search and commands" size="md" initialFocusRef={inputRef}>
      <Input
        ref={inputRef}
        type="text"
        role="combobox"
        aria-label="Search pages and commands"
        aria-expanded={visible.length > 0}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={visible.length > 0 ? optionId(index) : undefined}
        autoComplete="off"
        placeholder="Type a page or command"
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setActive(0);
        }}
        onKeyDown={onKeyDown}
      />
      <ul id={listId} role="listbox" aria-label="Results" className="m-0 mt-2 max-h-[50vh] list-none overflow-auto p-0">
        {visible.map((command, i) => (
          <li
            key={command.id}
            id={optionId(i)}
            role="option"
            aria-selected={i === index}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => choose(command)}
            onMouseEnter={() => setActive(i)}
            className={cx(
              "flex cursor-pointer items-center gap-3 rounded px-2.5 py-2 text-sm text-[var(--ua-fg)] max-md:min-h-11",
              i === index && "bg-[var(--ua-selected)]",
            )}
          >
            <span className="flex-1">{command.label}</span>
            {command.group && <span className="text-xs text-[var(--ua-muted)]">{command.group}</span>}
            {command.shortcut && (
              <kbd aria-hidden="true" className="font-mono text-[11px] text-[var(--ua-muted)]">
                {command.shortcut}
              </kbd>
            )}
          </li>
        ))}
      </ul>
      {visible.length === 0 && (
        <p role="status" className="m-0 mt-2 text-sm text-[var(--ua-muted)]">
          Nothing matches “{query}”.
        </p>
      )}
    </Dialog>
  );
}
