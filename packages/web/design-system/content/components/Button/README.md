Button is the one action control: a native `<button>` with four variants, three sizes and a busy state.

- **Use** `primary` once per view for the main action ("Review and publish"), `secondary` (the default) for everything else, `danger` for destructive or denying actions, `ghost` inside dense rows and toolbars.
- **Sizes:** `sm` (`control-sm`, in table rows), `md` (`control-md`, default), `lg` (`control-lg`, touch and sign-in).
- **Consumer provides:** `children` (a sentence-case verb phrase), `onClick`, optional `leftIcon` / `rightIcon` (decorative; hidden from assistive tech), `loading` while a mutation is in flight (sets `aria-busy`, disables, swaps the left icon for a spinner).
- Defaults to `type="button"`; pass `type="submit"` inside forms. All other button attributes pass through.
- **Don't** put two primaries side by side, or colour a button by state: a danger button means the action is destructive, not that something failed.
