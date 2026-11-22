/**
 * Signs that an approval's argument preview was shortened by the hub.
 *
 * `GET /api/approvals/{id}` returns `payload` through
 * `telemetry.redaction.sanitize_metadata`, which silently bounds what it
 * shows and does not (yet, hub task #2498) say when it did:
 *
 * - top level: the first 50 keys, each key cut to 64 characters;
 * - nested mappings: the first 10 keys, each key cut to 32 characters;
 * - lists: the first 10 items;
 * - strings: 200 characters at the top level and 100 below, cut to
 *   `max_len - 3` characters plus "...";
 * - depth 3 and below: containers are turned into their Python text form;
 * - the whole payload: keys are dropped from the end until the JSON is at
 *   most 8192 bytes, and if one key is still too big the payload becomes
 *   `{"_truncated": "telemetry metadata exceeded the payload limit"}`.
 *
 * A value exactly at a limit may be complete or may have been cut, so each
 * hit is a "may have been shortened", never a certainty.
 */
export const PAYLOAD_BYTE_LIMIT = 8192;
/** Below the byte cap, keys dropped from the end leave no trace; this close to it, assume they may have been. */
export const PAYLOAD_BYTE_MARGIN = 2048;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function label(path: string): string {
  return path || "the arguments";
}

export function previewLimitReasons(payload: unknown): string[] {
  if (!isPlainObject(payload)) return [];
  const reasons: string[] = [];
  if ("_truncated" in payload) {
    return ["The hub replaced the arguments with a note that they were too large to show."];
  }
  const keys = Object.keys(payload);
  if (keys.length >= 50) reasons.push("It shows 50 arguments, the most the hub shows; more may have been left out.");
  if (keys.some((k) => k.length >= 64)) reasons.push("An argument name is 64 characters long, the most the hub shows; it may be cut.");
  const bytes = new TextEncoder().encode(JSON.stringify(payload)).length;
  if (bytes > PAYLOAD_BYTE_LIMIT - PAYLOAD_BYTE_MARGIN) {
    reasons.push(`The preview is ${Math.round(bytes / 1024)} KB, near the hub's 8 KB limit; arguments at the end may have been left out.`);
  }

  const visit = (value: unknown, depth: number, path: string) => {
    if (typeof value === "string") {
      const max = depth === 0 ? 200 : 100;
      if (value.length >= max - 3 && value.endsWith("...")) {
        reasons.push(`${label(path)} is cut at ${max} characters and may continue.`);
      } else if (depth >= 3 && /^[[{(]/.test(value)) {
        reasons.push(`${label(path)} is nested too deeply for the hub to show its contents in full.`);
      }
      return;
    }
    if (Array.isArray(value)) {
      if (value.length === 10) reasons.push(`${label(path)} shows 10 items, the most the hub shows; there may be more.`);
      value.forEach((item, i) => visit(item, depth + 1, `${path}[${i}]`));
      return;
    }
    if (isPlainObject(value)) {
      const entries = Object.entries(value);
      // Every mapping inside an argument (the argument's own value included) is cut to 10 keys of 32 characters.
      if (entries.length === 10) reasons.push(`${label(path)} shows 10 fields, the most the hub shows; there may be more.`);
      if (entries.some(([k]) => k.length >= 32)) reasons.push(`A field name in ${label(path)} is 32 characters long and may be cut.`);
      for (const [k, item] of entries) visit(item, depth + 1, path ? `${path}.${k}` : k);
    }
  };
  for (const [k, item] of Object.entries(payload)) visit(item, 0, k);
  return reasons;
}

/**
 * Whether the approval read says its preview is complete. Today no read
 * does (hub task #2498 item h will add it); the two spellings are read
 * defensively so the standing acknowledgement can be dropped when it lands.
 */
export function previewSaysComplete(detail: unknown): boolean {
  if (!detail || typeof detail !== "object") return false;
  const d = detail as { preview_complete?: unknown; truncated?: unknown };
  return d.preview_complete === true || d.truncated === false;
}

/** A stable fingerprint of what the person reviewed: the approval and the arguments shown. */
export function previewFingerprint(approvalId: number, payload: unknown): string {
  const canonical = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.keys(value as object).sort().map((k) => [k, canonical((value as Record<string, unknown>)[k])]));
    }
    return value;
  };
  return `${approvalId}|${JSON.stringify(canonical(payload ?? {}))}`;
}
