# Single-tenant pilot appliance bundle

`scripts/provision-pilot` renders a local, private configuration bundle for one
pilot hub. It does not create a WorkOS organization or application, GCS bucket,
Linux account, tailnet node, or service. It does not install or start units or
change Tailscale Serve. Treat the output as a reviewed deployment input, not a
provisioned appliance.

## Render the bundle

Prerequisites:

- A dedicated host or VM and Linux service account for this pilot.
- A WorkOS organization and User Management application, with its organization
  ID, client ID, and an identity map from WorkOS user IDs to existing Unified-
  Agent agent labels.
- A separately operated HTTPS auth frontend with a functioning registered
  callback. The generated hub does not serve `/pilot/login` or
  `/pilot/callback`; its WorkOS leg accepts sessions issued elsewhere.
- A dedicated GCS bucket and service-account credentials scoped to the pilot's
  `ua-pilots/<slug>/memory` object prefix.
- A tailnet device with a stable HTTPS MagicDNS name ending in `.ts.net`.
- The `unified-agent` and `litestream` executables installed for the service
  account.

Store the identity map in a protected JSON file, for example:

```json
{"user_01ABCDEF":"Pilot_Operator"}
```

The value must be an existing agent label in the pilot's target roster. The
renderer checks label syntax but cannot inspect that roster offline. Render
using non-secret identifiers and the bucket root:

```sh
scripts/provision-pilot acme-pilot \
  --workos-organization-id org_... \
  --workos-client-id client_... \
  --workos-redirect-uri https://auth.example.ts.net/pilot/callback \
  --identity-map-file /secure/path/identity-map.json \
  --tailnet-origin https://pilot-device.example-tailnet.ts.net \
  --backup-bucket gs://dedicated-pilot-backups \
  --private-home /home/private-owner/.unified_agent
```

The default output is `~/.local/share/unified-agent/pilots/acme-pilot`. The
renderer validates the tailnet HTTPS origin, derives a pilot-specific GCS
prefix, refuses a bundle overlapping the private hub's `UA_HOME`, and refuses
to overwrite an existing bundle. Secret files and state directories are
private to the invoking user. Review `pilot.env`, then use a protected editor
to add `WORKOS_API_KEY` and `WORKOS_COOKIE_PASSWORD` to `secrets.env`. Keep any
webhook secret in the separately operated frontend's own secret store. The
`--workos-redirect-uri` value
must name the actual HTTPS callback on the separate auth frontend; the renderer
checks syntax but cannot verify that route or frontend. The main hub accepts
externally issued WorkOS sessions and does not implement interactive login.
For replication, set `GOOGLE_APPLICATION_CREDENTIALS` in `secrets.env` to an
absolute credential-file path readable by the dedicated service account.
Default per-user ADC under that account's normal home is not available because
the wrapper deliberately isolates `HOME`. Never put secret values in command
arguments, the identity map, or shell history.

The generated `with-secrets` wrapper allows only the two WorkOS
session-verification values into the hub process and only the GCS credential
path into Litestream. The hub gets
an explicit pilot environment and pilot-scoped `HOME` and `TMPDIR`; unrelated
inherited credentials, including private hub bearer tokens, are discarded.

## Operator activation and verification

Do not activate the bundle until the separate auth frontend, callback, and
pilot identity roster have been independently verified. Then perform these
steps on the dedicated pilot host, as the dedicated service account. Review
each rendered unit and path before installing it.

1. Install `systemd/*.service` under `~/.config/systemd/user/`, then run
   `systemctl --user daemon-reload` and inspect both units with
   `systemd-analyze --user verify` if supported by the host.
2. Start the hub first:
   `systemctl --user enable --now unified-agent-acme-pilot.service`.
   Verify `/healthz`, tenant organization identity, and the pilot's `UA_HOME`.
   Confirm a session from the separately operated auth frontend is accepted;
   the generated hub itself has no browser login or callback routes.
3. Start replication only after proving all four preconditions, or leave it
   off (the nightly snapshot is the supported recovery path). A missing
   delete grant is what cost about $65 a day in September 2026: Litestream
   could not prune, so it re-listed a 250,631-object prefix, and GCS billed
   every listing ([database operations](../database-operations.md), section 4).
   - The credential can DELETE under the replica URI, not only create: upload
     a probe object there with that credential, then delete it with the same
     credential. `storage.objectCreator` alone is not enough.
   - The bucket has a lifecycle rule scoped to that prefix (for example,
     delete objects older than 3 days), so a stalled prune cannot accumulate.
   - The bucket is in the pilot host's GCP region. Listings from outside
     Google Cloud are billed as internet egress.
   - The project's egress alert covers this bucket
     (`deploy/gcp/cost-guards/egress_policy.json`).
   Then `systemctl --user enable --now unified-agent-acme-pilot-litestream.service`.
   The rendered `litestream.yml` checks L0 retention every 5 minutes, not 15
   seconds. Inspect its logs and verify a fresh snapshot appears under the
   pilot's dedicated GCS prefix. Count the prefix's objects 30 minutes later
   and again the next day; a count that keeps growing means pruning is
   failing, so stop the unit. Confirm no other tenant's objects are readable
   by the pilot service identity.
4. On the pilot tailnet device, expose the loopback hub using
   `tailscale serve --bg --https=443 http://127.0.0.1:8765`. Confirm
   `tailscale serve status` shows the expected HTTPS MagicDNS name. Do not use
   Funnel or bind the hub to `0.0.0.0`.
5. Run a fresh `unified-agent doctor` as an operator and capture its output,
   exact runtime commit, hub health, external WorkOS session verification, and
   replica verification.
   An outsider should then rehearse the intended onboarding and work-product
   path from a device and account that are not the operator's.

Test isolation from the private hub explicitly: compare the private and pilot
`UA_HOME` values and confirm each uses distinct WorkOS organization mapping,
tailnet endpoint, service account, and backup prefix. Run the rehearsal using a
fresh pilot identity. Do not claim acceptance based solely on a rendered bundle
or a local unit test.

## Rollback and teardown evidence

Capture the pilot's exported data and retention decision before removing
resources. Stop and disable the two pilot units, remove only the pilot HTTPS
Serve mapping, revoke the pilot WorkOS credentials and GCS service-account
access, and remove the pilot's tailnet device and cloud resources through their
owning operators. Delete only the pilot's bundle and GCS prefix after the
retention decision is recorded. Preserve command output or operator records
showing each resource is gone and that the operator's private hub remains healthy.

The generated bundle is not proof that teardown succeeded. This PR does not
provide the external auth frontend, perform cloud provisioning, fresh-hub
doctor verification, isolation testing, an outsider rehearsal, or teardown.
Those live checks are required before task #1277 can be accepted.

## Related docs

- [WorkOS pilot configuration](../workos-pilot.md)
- [Support and rollback](support-and-rollback.md)
- [Pilot onboarding kit](README.md)
