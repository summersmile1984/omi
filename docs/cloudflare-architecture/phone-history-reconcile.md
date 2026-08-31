# Historical Phone/Twilio number reconciliation

`deploy/cloudflare/scripts/phone-history-reconcile.mjs` is the first safe
historical Phone slice. It is a permanent dry-run planner only: it does not
read Firestore or GCS, decrypt an export, call Twilio, or write Cloudflare.
The output is reviewable SQL for `cf_phone_number_import_ledger`, plus a
verification query. The separate Jobs executor can promote a reviewed ledger
row into `cf_phone_numbers`; this planner never promotes it itself.

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

## Export checksum and reviewed apply CLI

`deploy/cloudflare/scripts/phone-history-export-verify.mjs` closes the operator
handoff around the planner. It reads at most 8 MiB, requires the exact phone
export schema, computes SHA-256 over the original UTF-8 manifest bytes, and
optionally requires an independently recorded `--expected-sha256`. It then
invokes the existing planner and can perform the Jobs review/apply sequence
with `--apply`:

```sh
node deploy/cloudflare/scripts/phone-history-export-verify.mjs \
  --export /path/to/phone-history-export.json \
  --expected-sha256 <sha256-of-that-file>

ADMIN_KEY='...' node deploy/cloudflare/scripts/phone-history-export-verify.mjs \
  --export /path/to/phone-history-export.json \
  --expected-sha256 <sha256-of-that-file> \
  --apply https://jobs-staging.example/internal/phone-history/reviews
```

The file checksum and `source.export_sha256` are intentionally separate. The
latter is the independently attested checksum of the original Firestore
export, and phone `source_fingerprint` values are bound to it; requiring the
embedded source metadata to equal the checksum of the containing JSON would
create a self-referential checksum. The CLI reports both values and refuses
`--apply` without the file checksum. Its review request sends only
`manifest_sha256`, `uid`, `import_id`, and `plan_hash`; encrypted row payloads
and the admin key are never sent by the client. Apply is limited to the Jobs
Worker's 100-entry review bound and remains subject to
`PHONE_HISTORY_IMPORT_STAGING_ENABLED`.

## Reviewed apply (Jobs Worker)

Migration `0129_phone_number_import_executor.sql` adds a short-lived review
receipt and an immutable apply marker. It also records the planner's
`manifest_sha256` in each newly generated ledger row. The operator workflow is
deliberately two-step and uses the Jobs Worker directly with the `ADMIN_KEY`
`secret-key` header:

1. Apply the generated ledger SQL and inspect the bounded ledger export.
2. `POST /internal/phone-history/reviews` with
   `{ "manifest_sha256": "...", "entries": [{ "uid": "...", "import_id": "...", "plan_hash": "..." }] }`.
3. Review the returned opaque `review_id`, then
   `POST /internal/phone-history/reviews/{review_id}/apply`.

The `PHONE_HISTORY_IMPORT_STAGING_ENABLED` gate defaults to `false`. Review
requires every requested row to still be `stage`/`planned`, have the exact
manifest and plan hash, and contain only the bounded AES-GCM ciphertext shape.
Apply re-checks the destination-bound account generation and deletion fence,
rejects global hash/SID collisions or changed existing rows, and uses an
atomic D1 batch to insert `cf_phone_numbers` plus its apply marker. Repeating
the same review is idempotent. The executor never decrypts a number, returns
plaintext, reads Firestore/GCS, or calls Twilio.

This planner does not prove historical Firestore account identity continuity,
Twilio's current caller-ID state, data-protection key rotation, or exact
legacy response parity. Those remain required before Phone production owner
promotion.
