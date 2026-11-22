import { DescriptionList, Dialog } from "../../lib/ui";
import { SHORTCUTS } from "./useAppShortcuts";

/** `?`: every keyboard shortcut. */
export function ShortcutHelp({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog open={open} onClose={onClose} title="Keyboard shortcuts" description="Single keys work anywhere except while typing in a field.">
      <DescriptionList
        items={SHORTCUTS.map((s) => ({
          id: s.keys,
          term: <kbd className="font-mono text-[var(--ua-fg)]">{s.keys}</kbd>,
          value: s.does,
        }))}
      />
    </Dialog>
  );
}
