import React from "react";
import { UiLink } from "./link";

export type SortDirection = "ascending" | "descending";
export type SortState = { columnId: string; direction: SortDirection };

export interface Column<T> {
  id?: string;
  key?: string;
  header: React.ReactNode;
  accessor?: keyof T | ((row: T) => React.ReactNode);
  cell?: (row: T, index: number) => React.ReactNode;
  align?: "left" | "center" | "right";
  className?: string;
  headerClassName?: string;
  width?: string;
  /** Header becomes a sort button; requires `onSortChange` on the table. */
  sortable?: boolean;
  /** This column's cell holds the row link when `getRowHref` is set (default: the first column). */
  rowLink?: boolean;
}

export interface DataTableProps<T> {
  columns: Column<T>[];
  data: T[];
  keyField?: keyof T;
  getRowKey?: (row: T, index: number) => string | number;
  caption?: string;
  captionVisuallyHidden?: boolean;
  captionSrOnly?: boolean;
  emptyText?: string;
  renderEmpty?: () => React.ReactNode;
  onRowClick?: (row: T) => void;
  scrollLabel?: string;
  className?: string;
  isLoading?: boolean;
  /** Placeholder rows shown while loading with no data yet. */
  loadingRows?: number;
  /** Current sort, shown with `aria-sort` on the header. */
  sort?: SortState | null;
  onSortChange?: (sort: SortState) => void;
  /** Each row links here; the link sits in the `rowLink` column and a click anywhere on the row follows it. */
  getRowHref?: (row: T) => string;
  /** Keep the header visible while the body scrolls (set `maxHeight` too). */
  stickyHeader?: boolean;
  /** CSS max-height of the scroll region, e.g. "60vh". */
  maxHeight?: string;
  /** Row selection with a checkbox column. Needs `getRowKey` or `keyField`. */
  selection?: {
    selected: ReadonlySet<string>;
    onChange: (next: Set<string>) => void;
    /** Accessible name of a row's checkbox, e.g. the task title. */
    rowLabel: (row: T) => string;
  };
}

const CONTROLS = "a, button, input, select, textarea, label, summary, [role=button], [contenteditable=true]";

/** Whether a row click started on an interactive descendant rather than the row itself. */
function isFromControl(event: React.MouseEvent<HTMLTableRowElement>): boolean {
  const target = event.target as HTMLElement | null;
  if (!target || target === event.currentTarget) return false;
  const control = target.closest(CONTROLS);
  return control !== null && event.currentTarget.contains(control);
}

export function DataTable<T>({
  columns,
  data,
  keyField,
  getRowKey,
  caption,
  captionVisuallyHidden,
  captionSrOnly,
  emptyText,
  renderEmpty,
  onRowClick,
  scrollLabel,
  className = "",
  isLoading = false,
  loadingRows = 3,
  sort,
  onSortChange,
  getRowHref,
  stickyHeader = false,
  maxHeight,
  selection,
}: DataTableProps<T>): React.ReactElement {
  const keyOf = (row: T, index: number): string =>
    String(getRowKey ? getRowKey(row, index) : keyField ? row[keyField] : index);
  const linkColumnIndex = Math.max(
    0,
    columns.findIndex((c) => c.rowLink),
  );
  const allKeys = data.map((row, i) => keyOf(row, i));
  const selectedCount = selection ? allKeys.filter((k) => selection.selected.has(k)).length : 0;
  const headerCheckbox = React.useRef<HTMLInputElement>(null);
  React.useEffect(() => {
    if (headerCheckbox.current) {
      headerCheckbox.current.indeterminate = selectedCount > 0 && selectedCount < allKeys.length;
    }
  }, [selectedCount, allKeys.length]);
  const totalColumns = columns.length + (selection ? 1 : 0);
  const isCaptionHidden = captionVisuallyHidden ?? captionSrOnly ?? true;

  // True WAI-ARIA Roving Tabindex state
  const [activeRowIndex, setActiveRowIndex] = React.useState<number>(0);

  const safeActiveIndex =
    data.length > 0
      ? Math.min(Math.max(0, activeRowIndex), data.length - 1)
      : 0;

  const handleRowKeyDown = (
    e: React.KeyboardEvent<HTMLTableRowElement>,
    row: T,
    rowIdx: number
  ) => {
    // Prevent cell controls (buttons, inputs, links) from triggering row navigation
    if (e.target !== e.currentTarget) return;

    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onRowClick?.(row);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      const nextRow = e.currentTarget.nextElementSibling as HTMLElement | null;
      if (nextRow && nextRow.tagName === "TR") {
        setActiveRowIndex(rowIdx + 1);
        nextRow.focus();
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      const prevRow = e.currentTarget.previousElementSibling as HTMLElement | null;
      if (prevRow && prevRow.tagName === "TR") {
        setActiveRowIndex(rowIdx - 1);
        prevRow.focus();
      }
    } else if (e.key === "Home") {
      e.preventDefault();
      const tbody = e.currentTarget.parentElement;
      const firstRow = tbody?.firstElementChild as HTMLElement | null;
      if (firstRow && firstRow.tagName === "TR") {
        setActiveRowIndex(0);
        firstRow.focus();
      }
    } else if (e.key === "End") {
      e.preventDefault();
      const tbody = e.currentTarget.parentElement;
      const lastRow = tbody?.lastElementChild as HTMLElement | null;
      if (lastRow && lastRow.tagName === "TR") {
        setActiveRowIndex(data.length - 1);
        lastRow.focus();
      }
    }
  };

  const getAlignmentClass = (align?: "left" | "center" | "right") => {
    switch (align) {
      case "right":
        return "text-right";
      case "center":
        return "text-center";
      case "left":
      default:
        return "text-left";
    }
  };

  const wrapperClasses = [
    "overflow-x-auto rounded-md border border-[var(--ua-border)] bg-[var(--ua-surface)] focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] focus-visible:outline-offset-2",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      tabIndex={0}
      role="region"
      aria-label={scrollLabel ?? caption ?? "Data table"}
      className={wrapperClasses}
      style={maxHeight ? { maxHeight, overflowY: "auto" } : undefined}
    >
      <table
        className="w-full border-collapse text-left text-sm"
        aria-label={caption}
      >
        {caption && (
          <caption
            className={
              isCaptionHidden
                ? "sr-only"
                : "px-3 py-2 text-xs font-medium text-[var(--ua-muted)] border-b border-[var(--ua-border)] text-left"
            }
          >
            {caption}
          </caption>
        )}
        <thead
          className={[
            "bg-[var(--ua-surface)] text-[var(--ua-muted)] border-b border-[var(--ua-border)]",
            stickyHeader ? "sticky top-0 z-10" : "",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          <tr>
            {selection && (
              <th scope="col" className="w-10 px-3 py-2">
                <input
                  ref={headerCheckbox}
                  type="checkbox"
                  aria-label="Select all rows"
                  checked={allKeys.length > 0 && selectedCount === allKeys.length}
                  onChange={(event) =>
                    selection.onChange(new Set(event.target.checked ? allKeys : []))
                  }
                  className="size-4 cursor-pointer accent-[var(--ua-accent)] focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)]"
                />
              </th>
            )}
            {columns.map((col, idx) => {
              const alignClass = getAlignmentClass(col.align);
              const headerClasses = [
                "px-3 py-2 font-medium text-xs uppercase tracking-wider",
                alignClass,
                col.headerClassName,
              ]
                .filter(Boolean)
                .join(" ");

              const columnId =
                col.id ?? col.key ?? (typeof col.accessor === "string" ? col.accessor : String(idx));
              const isSorted = sort?.columnId === columnId;
              const direction = isSorted && sort ? sort.direction : null;
              const canSort = Boolean(col.sortable && onSortChange);
              return (
                <th
                  key={columnId}
                  scope="col"
                  className={headerClasses}
                  style={col.width ? { width: col.width } : undefined}
                  aria-sort={canSort ? (direction ?? "none") : undefined}
                >
                  {canSort ? (
                    <button
                      type="button"
                      onClick={() =>
                        onSortChange?.({
                          columnId,
                          direction: direction === "ascending" ? "descending" : "ascending",
                        })
                      }
                      className="inline-flex cursor-pointer items-center gap-1 border-0 bg-transparent p-0 text-inherit font-inherit uppercase tracking-wider focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)]"
                    >
                      {col.header}
                      <span aria-hidden="true">
                        {direction === "ascending" ? "▲" : direction === "descending" ? "▼" : "↕"}
                      </span>
                    </button>
                  ) : (
                    col.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {data.length === 0 && isLoading && loadingRows > 0 ? (
            Array.from({ length: loadingRows }, (_, i) => (
              <tr key={`loading-${i}`} className="border-t border-[var(--ua-border)] first:border-t-0">
                <td colSpan={totalColumns} className="px-3 py-2.5">
                  {i === 0 && (
                    <span className="sr-only" role="status">
                      Loading…
                    </span>
                  )}
                  <span aria-hidden="true" className="block h-3.5 w-3/4 animate-pulse rounded bg-[var(--ua-hover)]" />
                </td>
              </tr>
            ))
          ) : data.length === 0 ? (
            <tr>
              <td
                colSpan={totalColumns}
                className="p-6 text-center text-sm text-[var(--ua-muted)]"
              >
                {renderEmpty ? (
                  renderEmpty()
                ) : (
                  <p role="status" className="m-0">
                    {emptyText ?? "No data available"}
                  </p>
                )}
              </td>
            </tr>
          ) : (
            data.map((row, rowIdx) => {
              const rowKey = getRowKey
                ? getRowKey(row, rowIdx)
                : keyField
                ? String(row[keyField])
                : rowIdx;

              const href = getRowHref?.(row);
              const isClickable = Boolean(onRowClick);
              const isRowActive = rowIdx === safeActiveIndex;

              const rowClasses = [
                "border-t border-[var(--ua-border)] first:border-t-0",
                isClickable &&
                  "cursor-pointer hover:bg-[var(--ua-hover)] focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)] focus-visible:-outline-offset-2 transition-colors",
              ]
                .filter(Boolean)
                .join(" ");

              return (
                <tr
                  key={rowKey}
                  tabIndex={isClickable ? (isRowActive ? 0 : -1) : undefined}
                  className={[rowClasses, href && !isClickable ? "cursor-pointer hover:bg-[var(--ua-hover)]" : ""]
                    .filter(Boolean)
                    .join(" ")}
                  onClick={
                    isClickable
                      ? (event) => {
                          // A click on a control inside the row (selection checkbox,
                          // its label, a cell button or link) is that control's, not the row's.
                          if (isFromControl(event)) return;
                          setActiveRowIndex(rowIdx);
                          onRowClick!(row);
                        }
                      : href
                        ? (event) => {
                            // A click on the row follows its link, unless it hit a control.
                            if (isFromControl(event)) return;
                            event.currentTarget
                              .querySelector<HTMLAnchorElement>("a[data-row-link]")
                              ?.click();
                          }
                        : undefined
                  }
                  onFocus={
                    isClickable
                      ? (e) => {
                          if (e.target === e.currentTarget && safeActiveIndex !== rowIdx) {
                            setActiveRowIndex(rowIdx);
                          }
                        }
                      : undefined
                  }
                  onKeyDown={
                    isClickable ? (e) => handleRowKeyDown(e, row, rowIdx) : undefined
                  }
                >
                  {selection && (
                    <td className="w-10 px-3 py-2.5 align-middle">
                      <input
                        type="checkbox"
                        aria-label={`Select ${selection.rowLabel(row)}`}
                        checked={selection.selected.has(String(rowKey))}
                        onChange={(event) => {
                          const next = new Set(selection.selected);
                          if (event.target.checked) next.add(String(rowKey));
                          else next.delete(String(rowKey));
                          selection.onChange(next);
                        }}
                        className="size-4 cursor-pointer accent-[var(--ua-accent)] focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)]"
                      />
                    </td>
                  )}
                  {columns.map((col, colIdx) => {
                    let cellContent: React.ReactNode = null;
                    if (col.cell) {
                      cellContent = col.cell(row, rowIdx);
                    } else if (typeof col.accessor === "function") {
                      cellContent = col.accessor(row);
                    } else if (col.accessor) {
                      cellContent = row[col.accessor] as unknown as React.ReactNode;
                    }

                    if (href && colIdx === linkColumnIndex) {
                      cellContent = (
                        <UiLink
                          href={href}
                          data-row-link=""
                          className="text-[var(--ua-link)] underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-[var(--ua-accent)]"
                        >
                          {cellContent}
                        </UiLink>
                      );
                    }

                    const alignClass = getAlignmentClass(col.align);
                    const cellClasses = [
                      "px-3 py-2.5 text-[var(--ua-fg)] align-middle",
                      alignClass,
                      col.className,
                    ]
                      .filter(Boolean)
                      .join(" ");

                    return (
                      <td
                        key={col.id ?? col.key ?? (typeof col.accessor === "string" ? col.accessor : colIdx)}
                        className={cellClasses}
                      >
                        {cellContent}
                      </td>
                    );
                  })}
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}
