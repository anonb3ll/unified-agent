import { lazy, Suspense, useState } from "react";
import { Button } from "./Button";
import { Badge, Chip } from "./Badge";
import { Card } from "./Card";
import { DataTable, type Column } from "./DataTable";
import { StatusIndicator } from "./StatusIndicator";

// Loaded on demand so the showcase chunk stays small until the v2
// primitives (and the graph library) are needed.
const PrimitivesShowcase = lazy(() => import("./PrimitivesShowcase").then((m) => ({ default: m.PrimitivesShowcase })));

interface ShowcaseItem {
  id: string;
  name: string;
  role: string;
  status: string;
}

const DEMO_COLUMNS: Column<ShowcaseItem>[] = [
  { id: "id", header: "ID", accessor: "id" },
  { id: "name", header: "Name", accessor: "name" },
  { id: "status", header: "Status", cell: (row) => <StatusIndicator status={row.status} /> },
  {
    id: "action",
    header: "Action",
    cell: (row) => (
      <Button
        size="sm"
        variant="secondary"
        data-testid={`row-action-btn-${row.id}`}
        onClick={(e) => {
          e.stopPropagation();
        }}
      >
        Action {row.id}
      </Button>
    ),
  },
];

const DEMO_ROWS: ShowcaseItem[] = [
  { id: "1", name: "Desktop_Agent1", role: "Primary Agent", status: "running" },
  { id: "2", name: "Desktop_Agent2", role: "Developer Agent", status: "doing" },
  { id: "3", name: "Desktop_Agent5", role: "Coordinator", status: "idle" },
  { id: "4", name: "Task_950", role: "Design System", status: "done" },
  { id: "5", name: "Task_951", role: "Prototype", status: "blocked" },
];

export function UiShowcase() {
  const [cardClickCount, setCardClickCount] = useState(0);
  const [rowClickCount, setRowClickCount] = useState(0);
  const [chipRemoved, setChipRemoved] = useState(false);
  const [chipPressed, setChipPressed] = useState(false);

  return (
    <div data-testid="ui-showcase" className="p-8 space-y-12 max-w-6xl mx-auto">
      <header className="border-b border-[var(--ua-border)] pb-4">
        <h1 className="text-2xl font-bold text-[var(--ua-fg)]">UI Primitives Design System Showcase</h1>
        <p className="text-[var(--ua-muted)] text-sm">WCAG 2.2 AA Verification Surface for Playwright Chromium</p>
      </header>

      {/* Buttons Section */}
      <section data-testid="showcase-buttons" className="space-y-4">
        <h2 className="text-lg font-semibold text-[var(--ua-fg)]">1. Buttons (Variants, Sizes & States)</h2>
        <div className="flex flex-wrap items-center gap-4">
          <Button variant="primary" size="md" data-testid="btn-primary-md">Primary Button</Button>
          <Button variant="secondary" size="md" data-testid="btn-secondary-md">Secondary Button</Button>
          <Button variant="danger" size="md" data-testid="btn-danger-md">Danger Button</Button>
          <Button variant="ghost" size="md" data-testid="btn-ghost-md">Ghost Button</Button>
          <Button variant="primary" size="sm">Small</Button>
          <Button variant="primary" size="lg">Large</Button>
          <Button variant="primary" disabled>Disabled</Button>
          <Button variant="primary" loading>Loading</Button>
        </div>
      </section>

      {/* Badges & Chips Section */}
      <section data-testid="showcase-badges-chips" className="space-y-4">
        <h2 className="text-lg font-semibold text-[var(--ua-fg)]">2. Badges & Chips</h2>
        <div className="flex flex-wrap items-center gap-3">
          <Badge variant="neutral">Neutral</Badge>
          <Badge variant="info">Info</Badge>
          <Badge variant="success">Success</Badge>
          <Badge variant="warning">Warning</Badge>
          <Badge variant="danger">Danger</Badge>
        </div>
        <div className="flex flex-wrap items-center gap-3 pt-2">
          <Chip pressed={chipPressed} onClick={() => setChipPressed(!chipPressed)}>Toggle Chip</Chip>
          <Chip count={14}>With Count</Chip>
          {!chipRemoved && (
            <div data-testid="chip-removable" className="inline-block">
              <Chip onRemove={() => setChipRemoved(true)}>Active Filter</Chip>
            </div>
          )}
        </div>
      </section>

      {/* Cards Section */}
      <section data-testid="showcase-cards" className="space-y-4">
        <h2 className="text-lg font-semibold text-[var(--ua-fg)]">3. Cards (Static & Interactive)</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <Card as="article">
            <Card.Header>
              <Card.Title>Static Information Card</Card.Title>
              <Card.Description>Card description text</Card.Description>
            </Card.Header>
            <Card.Body>This is a standard informational card body.</Card.Body>
            <Card.Footer>Footer metadata</Card.Footer>
          </Card>

          <Card
            interactive
            role="region"
            aria-label="Interactive Card with Child Controls"
            data-testid="card-interactive"
            onClick={() => setCardClickCount((c) => c + 1)}
          >
            <Card.Header>
              <Card.Title>Interactive Card with Child Controls</Card.Title>
            </Card.Header>
            <Card.Body className="space-y-3">
              <p data-testid="card-click-count" className="text-sm font-medium">Card clicks: {cardClickCount}</p>
              <div>
                <label htmlFor="card-input" className="block text-xs text-[var(--ua-muted)] mb-1">Nested Input</label>
                <input
                  id="card-input"
                  data-testid="card-nested-input"
                  type="text"
                  placeholder="Type space here..."
                  className="px-2 py-1 border border-[var(--ua-border)] bg-[var(--ua-bg)] text-[var(--ua-fg)] rounded text-sm"
                />
              </div>
            </Card.Body>
          </Card>
        </div>
      </section>

      {/* DataTable Section */}
      <section data-testid="showcase-datatable" className="space-y-4">
        <h2 className="text-lg font-semibold text-[var(--ua-fg)]">4. DataTable (Roving Tabindex & Event Isolation)</h2>
        <p data-testid="row-click-count" className="text-sm font-medium">Row clicks: {rowClickCount}</p>
        <div data-testid="interactive-datatable">
          <DataTable
            columns={DEMO_COLUMNS}
            data={DEMO_ROWS}
            keyField="id"
            caption="Interactive Roving Tabindex Table"
            onRowClick={() => setRowClickCount((r) => r + 1)}
          />
        </div>
      </section>

      {/* StatusIndicator Section */}
      <section data-testid="showcase-statuses" className="space-y-4">
        <h2 className="text-lg font-semibold text-[var(--ua-fg)]">5. Status Indicators (Lifecycle & Semantic Truth)</h2>
        <div className="flex flex-wrap items-center gap-6">
          <StatusIndicator status="running" label="Running (ok)" pulse />
          <StatusIndicator status="doing" label="Doing (accent)" />
          <StatusIndicator status="waiting" label="Waiting (accent)" />
          <StatusIndicator status="error" label="Error (warn)" />
          <StatusIndicator status="blocked" label="Blocked (warn)" />
          <StatusIndicator status="idle" label="Idle (muted)" />
          <StatusIndicator status="done" label="Done (ok)" />
          <StatusIndicator status="ok" label="Ok (ok)" />
          <StatusIndicator status="approved" label="Approved (ok)" />
          <StatusIndicator status="completed" label="Completed (ok)" />
          <div data-testid="status-null">
            <StatusIndicator status={null as any} />
          </div>
        </div>
      </section>
      <Suspense fallback={<p role="status">Loading more primitives…</p>}>
        <PrimitivesShowcase />
      </Suspense>
    </div>
  );
}
