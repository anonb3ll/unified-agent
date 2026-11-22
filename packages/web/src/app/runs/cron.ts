/**
 * Five-field UTC cron, exactly as the hub reads it (`routines._field`,
 * `_cron_fields`, `cron_next`), so the form can refuse what the hub would
 * refuse and show the next run times before anything is saved.
 *
 * Note the hub's rule for days: a minute matches only when BOTH the
 * day-of-month and the day-of-week fields match (classic cron uses "either"
 * when both are restricted). The description says so when it matters.
 */

export type CronFields = { minute: Set<number>; hour: Set<number>; day: Set<number>; month: Set<number>; weekday: Set<number> };
export type CronParse = { ok: true; fields: CronFields } | { ok: false; error: string };

const RANGES: [string, number, number][] = [
  ["minute", 0, 59],
  ["hour", 0, 23],
  ["day of the month", 1, 31],
  ["month", 1, 12],
  ["day of the week", 0, 7],
];

/** Python `int()` on a cron token: optional sign, digits, surrounding spaces. */
function pyInt(text: string): number | null {
  const t = text.trim();
  return /^[+-]?\d+$/.test(t) ? Number.parseInt(t, 10) : null;
}

function parseField(spec: string, low: number, high: number, name: string): Set<number> | string {
  const values = new Set<number>();
  for (const part of spec.split(",")) {
    const slash = part.indexOf("/");
    const base = slash >= 0 ? part.slice(0, slash) : part;
    const stepText = slash >= 0 ? part.slice(slash + 1) : "";
    const hasStep = slash >= 0;
    const stepDigits = /^\d+$/.test(stepText);
    const step = hasStep && stepDigits ? Number(stepText) : 1;
    if (step < 1 || (hasStep && !stepDigits)) return `The step after "/" in the ${name} must be a whole number of 1 or more.`;
    let first: number;
    let last: number;
    if (base === "*") {
      first = low;
      last = high;
    } else if (base.includes("-")) {
      const dash = base.indexOf("-");
      const a = pyInt(base.slice(0, dash));
      const b = pyInt(base.slice(dash + 1));
      if (a === null || b === null) return `The ${name} has a range that is not two numbers.`;
      first = a;
      last = b;
    } else {
      const n = pyInt(base);
      if (n === null) return `The ${name} must be a number, a range like 1-5, a list like 1,15, or *.`;
      first = n;
      last = hasStep ? high : n;
    }
    if (!(low <= first && first <= last && last <= high)) return `The ${name} must be between ${low} and ${high}.`;
    for (let v = first; v <= last; v += step) values.add(v);
  }
  if (values.size === 0) return `The ${name} matches nothing.`;
  return values;
}

export function parseCron(schedule: string): CronParse {
  const parts = schedule.trim().split(/\s+/).filter(Boolean);
  if (parts.length !== 5) return { ok: false, error: "A schedule has five parts separated by spaces: minute, hour, day of the month, month, day of the week." };
  const out: Set<number>[] = [];
  for (let i = 0; i < 5; i++) {
    const [name, low, high] = RANGES[i];
    const field = parseField(parts[i], low, high, name);
    if (typeof field === "string") return { ok: false, error: field };
    out.push(field);
  }
  const [minute, hour, day, month, weekday] = out;
  return { ok: true, fields: { minute, hour, day, month, weekday } };
}

/** Next matching UTC minutes strictly after `after` (`routines.cron_next`, repeated). */
export function nextRuns(fields: CronFields, after: Date, count: number): Date[] {
  const out: Date[] = [];
  let from = after;
  for (let i = 0; i < count; i++) {
    const next = cronNext(fields, from);
    if (!next) break;
    out.push(next);
    from = next;
  }
  return out;
}

const MINUTE = 60_000;
const DAY = 86_400_000;

export function cronNext(fields: CronFields, after: Date): Date | null {
  let c = Math.floor(after.getTime() / MINUTE) * MINUTE + MINUTE;
  const deadline = c + 146_097 * DAY;
  while (c <= deadline) {
    const d = new Date(c);
    if (!fields.month.has(d.getUTCMonth() + 1)) {
      // First day of the next month, 00:00.
      c = Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1);
      continue;
    }
    const weekday = d.getUTCDay(); // 0 = Sunday, as cron
    if (!fields.day.has(d.getUTCDate()) || !(fields.weekday.has(weekday) || (weekday === 0 && fields.weekday.has(7)))) {
      c = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1);
      continue;
    }
    if (!fields.hour.has(d.getUTCHours())) {
      c = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), d.getUTCHours() + 1);
      continue;
    }
    if (fields.minute.has(d.getUTCMinutes())) return d;
    c += MINUTE;
  }
  return null;
}

/** "Mon 16 Nov 2026, 09:00 UTC (10:00 your time)". */
export function describeRunTime(date: Date, timeZone?: string): string {
  const utc = new Intl.DateTimeFormat("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  }).format(date);
  const local = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", weekday: "short", ...(timeZone ? { timeZone } : {}) }).format(date);
  return `${utc} UTC (${local} your time)`;
}

/** Whether the day-of-month and day-of-week fields are both restricted (the hub then needs both to match). */
export function bothDayFieldsRestricted(schedule: string): boolean {
  const parts = schedule.trim().split(/\s+/);
  return parts.length === 5 && parts[2] !== "*" && parts[4] !== "*";
}
