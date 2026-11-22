/**
 * Leaving the Control Center for a provider's sign-in page: a full-page
 * navigation (spec §9.8). Kept in its own module so tests can replace it.
 * Call only with a URL that `checkAuthorizeUrl` accepted.
 */
export function leaveForProvider(url: string): void {
  window.location.assign(url);
}
