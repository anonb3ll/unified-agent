import { Button } from "../../lib/ui";
import type { Attempt } from "./attempts";

/**
 * A write that was sent from here and whose answer never arrived (the person
 * left, or the tab reloaded). It may or may not have been made, so the form
 * is not offered again until the person has checked.
 */
export function AttemptNotice({
  attempt,
  onMade,
  onNotMade,
}: {
  attempt: Attempt;
  /** It was made: the form's draft is spent. */
  onMade: () => void;
  /** It was not made: the form is offered again. */
  onNotMade: () => void;
}) {
  return (
    <section aria-label="Outcome unknown" className="flex flex-col gap-2 rounded-md border border-l-4 border-[var(--ua-warn)] px-3 py-2 text-sm">
      <p className="m-0">
        <strong>Outcome unknown.</strong> {attempt.label} was sent, but its answer did not arrive here, so it is not known
        whether it was made. Check before doing it again.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={onMade}>
          It is there
        </Button>
        <Button size="sm" onClick={onNotMade}>
          It did not happen
        </Button>
      </div>
    </section>
  );
}
