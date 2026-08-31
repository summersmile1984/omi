# Historical Phone/Twilio number reconciliation

`deploy/cloudflare/scripts/phone-history-reconcile.mjs` is the first safe
historical Phone slice. It is a permanent dry-run planner only: it does not
read Firestore or GCS, decrypt an export, call Twilio, or write Cloudflare.
The output is reviewable SQL for `cf_phone_number_import_ledger`, plus a
verification query. A future executor must re-check the account fence and
promote a reviewed ledger row into `cf_phone_numbers`; this planner never
promotes it itself.

## Required manifest

The input is a bounded schema-v1 manifest with the exact Firestore collection
marker and a SHA-256 checksum for the export:

```json
{
  "schema_version": 1,
  "source": {
    "kind": "firestore",
    "collection": "users/{uid}/phone_numbers",
    "ciphertext_scheme": "cloudflare-phone-aes-gcm-v1",
    "proof_scheme": "sha256-v1",
    "export_sha256": "<64 lowercase hex characters>"
  },
  "rows": [
    {
      "uid": "user-1",
      "source_record_id": "phone-1",
      "phone_number_id": "phone-1",
      "phone_number_hash": "<sha256 of canonical E.164>",
      "phone_number_ciphertext": "<base64url 12-byte IV>.<base64url AES-GCM payload>",
      "source_fingerprint": "<64 lowercase hex characters>",
      "proof": {
        "kind": "verified-e164",
        "method": "twilio-outgoing-caller-id",
        "canonicalization": "E.164",
        "verified": true,
        "value_sha256": "<same phone_number_hash>",
        "source_fingerprint": "<same source_fingerprint>",
        "proof_sha256": "<sha256 of the proof fields>",
        "attested_at": 1700000000
      },
      "twilio_sid": "PN...",
      "verified_at": 1700000000,
      "is_primary": true,
      "account_generation": 3,
      "created_at": 1700000000,
      "updated_at": 1700000001
    }
  ]
}
```

The planner accepts only the Cloudflare AES-GCM ciphertext shape (12-byte
nonce, authenticated payload, canonical unpadded base64url), a lowercase
SHA-256 hash, a non-negative destination account generation, and a proof whose
hash, method, canonicalization, and deterministic `proof_sha256` all agree.
The plaintext E.164 value must not occur anywhere in a row; raw phone fields,
pending/unverified status, credentials, and missing or mismatched proof block a
row or fail the run. The planner does not claim that a self-supplied hash is
itself evidence: `source.export_sha256` and the proof must come from the
independently reviewed export/reencryption process.

Rows are deduplicated by `(uid, source_record_id)`. Conflicting source rows,
global phone-hash claims, and global Twilio SID claims are blocked before SQL
generation. No blocked row is emitted as a staged insert. `cf_phone_number_import_ledger`
also enforces those uniqueness constraints in D1.

## Dry run and verification

```sh
node deploy/cloudflare/scripts/phone-history-reconcile.mjs \
  --input /path/to/phone-history-manifest.json \
  --fenced-uid <uid-being-deleted>
```

The generated SQL inserts only a `planned` ledger row and requires a completed,
destination-bound `cf_account_cutover` with the exact row generation. Both
the deletion intent and active tombstone are checked in the `INSERT ... SELECT`
guard. Re-running the same SQL is idempotent for the same plan hash and cannot
overwrite an existing ledger plan.

After an operator obtains a bounded D1 ledger export, verify it without
exposing phone plaintext:

```sh
node deploy/cloudflare/scripts/phone-history-reconcile.mjs \
  --input phone-history-plan.json \
  --verify --actual phone-history-ledger-export.json
```

`tests/phone-history-reconcile.test.ts` covers valid attested ciphertext,
plaintext/pending/missing-proof rejection, duplicate and hash/SID collision
blocking, SQL idempotency, generation admission, and deletion-fence absence.
The test suite is hermetic and never calls Twilio or a storage service.

This planner does not prove historical Firestore account identity continuity,
Twilio's current caller-ID state, data-protection key rotation, or exact
legacy response parity. Those remain required before Phone production owner
promotion.
