# Persona/App historical replay planner

`deploy/cloudflare/scripts/persona-app-history-reconcile.mjs` is the first
bounded planning seam for replaying the legacy Firestore `plugins_data` app
catalog into Cloudflare D1/R2. It is intentionally a dry-run tool: it reads a
local JSON export only, does not import a Firestore or GCS client, makes no
network request, and emits no executable SQL or remote write.

## Input contract

The input is a schema-v1 manifest:

```json
{
  "schema_version": 1,
  "source": {
    "kind": "firestore",
    "collection": "plugins_data",
    "export_sha256": "<64 lowercase hex characters>"
  },
  "rows": [
    {
      "source_uid": "fb-anon-<64 lowercase hex characters>",
      "uid": "better-auth-user",
      "app_id": "persona-1",
      "source_projection_revision": "<bounded revision>",
      "source_fingerprint": "<64 lowercase hex characters>",
      "target_account_generation": 7,
      "target_cutover": {
        "state": "new",
        "checkpoint_phase": "completed",
        "destination_backend_bound": true,
        "deletion_fenced": false
      },
      "public_metadata": {
        "id": "persona-1",
        "name": "Public metadata only",
        "capabilities": ["persona"]
      },
      "private_envelope": "v1.<base64url iv>.<base64url ciphertext>",
      "image_object": {
        "source_object_uri": "gs://bucket/path/logo.png",
        "source_generation": "<GCS generation>",
        "checksum_sha256": "<64 lowercase hex characters>",
        "size": 128,
        "content_type": "image/png"
      },
      "created_at": 0,
      "updated_at": 0
    }
  ]
}
```

`source_uid` must already be the hash-only identity-projection reference
(`fb-anon-<hash>`). A raw Firebase UID is rejected and is never echoed into
the plan. The export checksum, per-row source fingerprint, source projection
revision, target account generation, and completed destination-bound cutover
attestation are required. `deletion_fenced` must be explicitly false; the
`--fenced-uid` option can additionally block owners observed in a fresh fence
snapshot.

`public_metadata` is bounded and recursively checked. Prompt, Twitter,
credential, owner, email, and image fields are rejected rather than copied.
Private prompt/Twitter/image material can only be represented by an opaque
AES-GCM v1 envelope. The planner records only the envelope format, key version,
and SHA-256 in its output; ciphertext is not printed. A legacy logo is an
independent, credential-free `gs://` source descriptor with generation and
checksum. The planner emits a descriptive R2 copy operation and derives an
owner-scoped destination key; it never downloads or copies bytes.

## Planning and verification

```bash
node deploy/cloudflare/scripts/persona-app-history-reconcile.mjs \
  --input /path/to/plugins-data-manifest.json \
  --fenced-uid <uid>
```

The output includes a manifest checksum, per-row checksum, deterministic
idempotency key, public D1 operation preview, private-envelope metadata, and
R2 copy preview. It deduplicates identical `(target uid, app id)` rows and
blocks conflicting duplicates. A source app mapped to more than one target is
also blocked. `operations` is a descriptive report, not SQL and cannot be
passed directly to `wrangler d1 execute`.

`verifyPersonaAppHistory(plan, actualRows)` is available to a later offline
verification step. It compares an operator-exported D1 catalog snapshot by
owner, app id, generation, and canonical public metadata, and reports missing,
drifted, or duplicate rows. The apply implementation is intentionally not
part of this slice: it must re-check the account generation/deletion fence and
provision the private-envelope key authority at commit time.

## Current migration boundary

The existing `backfill-d1.mjs` safely handles public `cf_app_catalog.data_json`
only and rejects private fields; it cannot map the Firestore `plugins_data`
authority, private persona prompt, Twitter state, or legacy GCS logo to the
Cloudflare owner. This planner makes that gap auditable without claiming that
historical data has been replayed. Firebase identity continuity, memory
re-encryption, provider/cache side effects, and production cutover remain
separate gates.

