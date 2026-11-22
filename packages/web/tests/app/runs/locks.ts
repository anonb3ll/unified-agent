/**
 * A minimal Web Locks API for jsdom (`navigator.locks` is missing there):
 * exclusive locks, `ifAvailable` (granted null when held), waiting in order
 * otherwise, and `query()` for held names. Chromium in the Playwright specs
 * uses the real one.
 */
type Lock = { name: string };

export class FakeLockManager {
  held = new Set<string>();
  private queues = new Map<string, Promise<void>>();

  async request(name: string, options: { ifAvailable?: boolean }, callback: (lock: Lock | null) => Promise<unknown>): Promise<unknown> {
    if (options.ifAvailable && this.held.has(name)) return callback(null);
    const before = this.queues.get(name) ?? Promise.resolve();
    let release: () => void = () => {};
    const mine = new Promise<void>((r) => (release = r));
    this.queues.set(name, before.then(() => mine));
    await before;
    // A name marked held from outside (a test standing in for another tab) waits until it is released.
    while (this.held.has(name)) await new Promise((r) => setTimeout(r, 5));
    this.held.add(name);
    try {
      return await callback({ name });
    } finally {
      this.held.delete(name);
      release();
    }
  }

  async query() {
    return { held: [...this.held].map((name) => ({ name })) };
  }
}

/** Install a fresh fake (or remove it with `null`); returns it. */
export function installLocks(manager: FakeLockManager | null = new FakeLockManager()): FakeLockManager | null {
  Object.defineProperty(navigator, "locks", { value: manager ?? undefined, configurable: true, writable: true });
  return manager;
}
