/**
 * WebAuthn helpers for approver passkey registration (task #1322). The hub
 * sends creation options as JSON with base64url byte fields; the browser API
 * takes ArrayBuffers and returns them. Nothing here talks to the hub: the
 * Hosts page sends both requests through `useHubWrite`.
 */

/** Creation options as the hub sends them (`POST /api/oauth/passkeys/options`). */
export type CreationOptionsJSON = {
  challenge: string;
  rp: { id: string; name: string };
  user: { id: string; name: string; displayName: string };
  pubKeyCredParams: { type: "public-key"; alg: number }[];
  excludeCredentials?: { type: "public-key"; id: string }[];
  authenticatorSelection?: AuthenticatorSelectionCriteria;
  attestation?: AttestationConveyancePreference;
  timeout?: number;
};

/** The registration the hub verifies (`POST /api/oauth/passkeys`). */
export type PasskeyRegistration = {
  credential_id: string;
  client_data_json: string;
  authenticator_data: string;
  public_key: string;
  public_key_algorithm: number;
  label: string;
};

export function b64urlToBuffer(value: string): ArrayBuffer {
  const b64 = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob(padded);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out.buffer;
}

export function bufferToB64url(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let raw = "";
  for (let i = 0; i < bytes.length; i++) raw += String.fromCharCode(bytes[i]);
  return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function passkeysSupported(): boolean {
  return typeof window !== "undefined" && typeof window.PublicKeyCredential === "function" && Boolean(navigator.credentials?.create);
}

export function creationOptions(options: CreationOptionsJSON): PublicKeyCredentialCreationOptions {
  return {
    challenge: b64urlToBuffer(options.challenge),
    rp: options.rp,
    user: { ...options.user, id: b64urlToBuffer(options.user.id) },
    pubKeyCredParams: options.pubKeyCredParams,
    excludeCredentials: (options.excludeCredentials ?? []).map((c) => ({ type: c.type, id: b64urlToBuffer(c.id) })),
    ...(options.authenticatorSelection ? { authenticatorSelection: options.authenticatorSelection } : {}),
    ...(options.attestation ? { attestation: options.attestation } : {}),
    ...(options.timeout ? { timeout: options.timeout } : {}),
  };
}

/** Ask this device's authenticator for a new passkey, and build the body the hub verifies. Throws a plain message. */
export async function createPasskey(options: CreationOptionsJSON, label: string): Promise<PasskeyRegistration> {
  if (!passkeysSupported()) throw new Error("This browser does not support passkeys.");
  let credential: PublicKeyCredential | null;
  try {
    credential = (await navigator.credentials.create({ publicKey: creationOptions(options) })) as PublicKeyCredential | null;
  } catch (exc) {
    // NotAllowedError: the person dismissed the prompt or it timed out.
    if (exc instanceof DOMException && exc.name === "NotAllowedError") throw new Error("Passkey creation was cancelled or timed out.");
    if (exc instanceof DOMException && exc.name === "InvalidStateError") throw new Error("This device already has a passkey for this approver.");
    throw new Error(exc instanceof Error ? exc.message : "The passkey could not be created.");
  }
  if (!credential) throw new Error("Passkey creation was cancelled.");
  const response = credential.response as AuthenticatorAttestationResponse;
  const publicKey = response.getPublicKey();
  if (!publicKey) throw new Error("This authenticator did not return a supported public key.");
  return {
    credential_id: bufferToB64url(credential.rawId),
    client_data_json: bufferToB64url(response.clientDataJSON),
    authenticator_data: bufferToB64url(response.getAuthenticatorData()),
    public_key: bufferToB64url(publicKey),
    public_key_algorithm: response.getPublicKeyAlgorithm(),
    label,
  };
}

/** The Hosts page on the hub's configured issuer origin, where a passkey can be registered; null if the origin is unusable. */
export function issuerHostsLink(origin: string | null | undefined): string | null {
  if (!origin) return null;
  try {
    return new URL("/ui/agents/hosts#approver-passkeys", origin).toString();
  } catch {
    return null;
  }
}
