/**
 * sessionStorage access that never throws: a private window, blocked site
 * data or a full quota reads as empty and drops the write.
 */
export function readSession<T>(key: string, fallback: T): T {
  try {
    const raw = window.sessionStorage.getItem(key);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function writeSession(key: string, value: unknown): void {
  try {
    window.sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable: the value lives only in memory for this render */
  }
}

export function removeSession(key: string): void {
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

export function sessionKeys(prefix: string): string[] {
  try {
    const keys: string[] = [];
    for (let i = 0; i < window.sessionStorage.length; i++) {
      const key = window.sessionStorage.key(i);
      if (key?.startsWith(prefix)) keys.push(key);
    }
    return keys;
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------- raw areas
//
// The only module that may use these is `epoch.tsx`, which scopes every key
// under the verified owner (`epoch.storage`, `epoch.sharedStorage`). The
// unscoped helpers above serve only the pre-sign-in return path
// (`returnTo.ts`). The architecture test (tests/app/architecture.test.ts)
// enforces both.

export type StorageArea = "session" | "local";

function area(which: StorageArea): Storage {
  return which === "local" ? window.localStorage : window.sessionStorage;
}

export function readRaw(which: StorageArea, key: string): string | null {
  try {
    return area(which).getItem(key);
  } catch {
    return null;
  }
}

/** Write and read back: true only when storage now holds exactly `value`. */
export function writeRaw(which: StorageArea, key: string, value: string): boolean {
  try {
    area(which).setItem(key, value);
    return area(which).getItem(key) === value;
  } catch {
    return false;
  }
}

/** Remove and read back: true only when the key is gone. */
export function removeRaw(which: StorageArea, key: string): boolean {
  try {
    area(which).removeItem(key);
    return area(which).getItem(key) === null;
  } catch {
    return false;
  }
}

export function keysRaw(which: StorageArea, prefix: string): string[] {
  try {
    const store = area(which);
    const keys: string[] = [];
    for (let i = 0; i < store.length; i++) {
      const key = store.key(i);
      if (key?.startsWith(prefix)) keys.push(key);
    }
    return keys;
  } catch {
    return [];
  }
}
