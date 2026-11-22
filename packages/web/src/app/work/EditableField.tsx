/**
 * One task field edited in place: the value with an Edit button; editing
 * shows the control with Save and Cancel. Save sends one
 * `POST /api/action/task` with `expected_updated_at`, so an edit made over a
 * stale copy is refused (409) instead of overwriting someone else's change.
 */
import { useCallback, useEffect, useRef, type ReactNode } from "react";
import { useDraft } from "../lib/drafts";
import { Button } from "../../lib/ui";
import { useHubWrite } from "../lib/mutations";
import { WRITE_PATHS, type TaskRow, type TaskUpdateBody } from "./api";
import { useIdentity, WriteFeedback } from "./shared";

export type EditorProps<V> = { value: V; onChange: (value: V) => void; id: string };

/**
 * One task update. Every body carries `expected_updated_at`: the caller's own base when it has one
 * (an open editor), else the snapshot the page shows, so a write over a stale copy answers 409.
 */
export function useTaskUpdate(task: TaskRow, label: string, successMessage?: string) {
  const write = useHubWrite<TaskUpdateBody, TaskRow>({
    path: WRITE_PATHS.taskAction,
    invalidates: [["work"]],
    label,
    successMessage: successMessage ?? `${label}: saved.`,
  });
  const snapshot = task.updated_at ?? null;
  const run = useCallback(
    (body: TaskUpdateBody, overrides?: Parameters<typeof write.run>[1]) =>
      write.run(body.expected_updated_at === undefined && snapshot ? { ...body, expected_updated_at: snapshot } : body, overrides),
    [write.run, snapshot],
  );
  return { ...write, run };
}

export function EditableField<V>({
  task,
  name,
  label,
  display,
  initial,
  editor,
  toBody,
  editLabel,
}: {
  task: TaskRow;
  name: string;
  label: string;
  display: ReactNode;
  initial: V;
  editor: (props: EditorProps<V>) => ReactNode;
  /** The update fields for this value (without `id` and `expected_updated_at`). */
  toBody: (value: V) => Omit<TaskUpdateBody, "id">;
  editLabel?: string;
}) {
  // The open editor and its text are a draft (spec §6): they survive an error, a
  // parked approval and leaving the page, and are cleared on save or cancel.
  const identity = useIdentity();
  // `basedOn` is the task's updated_at when editing began and `original` the value then. A live
  // refresh never moves them: only the person's explicit choice below does, so a save is always
  // checked against what they saw (expected_updated_at), never against someone else's change.
  const draft = useDraft<{ editing: boolean; value: V; basedOn: string | null; original: V }>({
    identity,
    operation: `task.edit.${name}`,
    object: { id: task.id },
    initial: { editing: false, value: initial, basedOn: task.updated_at ?? null, original: initial },
  });
  const editing = draft.value.editing;
  const value = draft.value.value;
  const setValue = (next: V) => draft.setValue((prev) => ({ ...prev, value: next }));
  const setEditing = (open: boolean) =>
    open ? draft.setValue({ editing: true, value: initial, basedOn: task.updated_at ?? null, original: initial }) : draft.clear();
  const changedUnderneath = editing && JSON.stringify(draft.value.original) !== JSON.stringify(initial);
  const rebase = (keepMine: boolean) =>
    draft.setValue((prev) => ({ ...prev, value: keepMine ? prev.value : initial, original: initial, basedOn: task.updated_at ?? null }));
  const write = useTaskUpdate(task, `${label} of task #${task.id}`, `${label} saved.`);
  const id = `task-${task.id}-${name}`;
  const editButton = useRef<HTMLButtonElement>(null);
  const wasEditing = useRef(false);

  // Opening moves focus into the control; closing (Save, Cancel, Escape) returns it to Edit.
  useEffect(() => {
    if (editing) document.getElementById(id)?.focus();
    else if (wasEditing.current) editButton.current?.focus();
    wasEditing.current = editing;
  }, [editing, id]);

  const cancel = () => {
    setEditing(false);
    write.reset();
  };

  const save = async (base?: string | null) => {
    const body: TaskUpdateBody = { id: task.id, ...toBody(value) };
    const basedOn = base ?? draft.value.basedOn ?? task.updated_at;
    if (basedOn) body.expected_updated_at = basedOn;
    const outcome = await write.run(body);
    if (outcome.kind === "done" || outcome.kind === "accepted") setEditing(false);
  };

  return (
    <div
      className="flex flex-col gap-1.5"
      onKeyDown={(event) => {
        // A control that uses Escape itself (an open list) prevents the default first.
        if (editing && event.key === "Escape" && !event.defaultPrevented && !write.isRunning) {
          event.preventDefault();
          cancel();
        }
      }}
    >
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          {editing ? (
            <>
              <label htmlFor={id} className="sr-only">
                {label}
              </label>
              {editor({ value, onChange: setValue, id })}
            </>
          ) : (
            display
          )}
        </div>
        {editing ? (
          <div className="flex gap-1.5">
            <Button size="sm" variant="primary" loading={write.isRunning} onClick={() => void save()}>
              Save
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={write.isRunning}
              onClick={cancel}
            >
              Cancel
            </Button>
          </div>
        ) : (
          <Button
            ref={editButton}
            size="sm"
            variant="ghost"
            aria-label={editLabel ?? `Edit ${label.toLowerCase()}`}
            onClick={() => {
              write.reset();
              setEditing(true);
            }}
          >
            Edit
          </Button>
        )}
      </div>
      {changedUnderneath && (
        <div role="alert" className="flex flex-col gap-2 rounded-md border border-[var(--ua-warn)] px-3 py-2 text-sm">
          <p className="m-0">
            <strong>This changed while you were editing.</strong> Someone else saved a new {label.toLowerCase()}. Saving now is refused until you choose.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => rebase(true)}>
              Keep my text and save over theirs
            </Button>
            <Button size="sm" variant="ghost" onClick={() => rebase(false)}>
              Use their version
            </Button>
          </div>
        </div>
      )}
      <WriteFeedback
        state={write.state}
        what={`The ${label.toLowerCase()} change`}
        // A 409 is resolved against the reloaded task (review round 3, item 7): Retry saves this value over the
        // task as it is now, with that version as the base. If this very field changed too, the choice above comes first.
        onRetry={
          editing
            ? () => {
                const base = task.updated_at ?? null;
                draft.setValue((prev) => ({ ...prev, basedOn: base, original: initial }));
                return save(base);
              }
            : undefined
        }
        retryBlocked={changedUnderneath ? `The ${label.toLowerCase()} itself changed: choose which version to keep above first.` : null}
      />
    </div>
  );
}
