# Cloudflare memory privacy receipt admission — 2026-09-07

All current Core memory creators and the Jobs X extractor now persist the same
server-keyed uid/item receipt identity. The HMAC domain and output match the
unchanged upstream `privacy_deletion_receipt_id` function. Python and TypeScript
consume shared ASCII/Unicode fixtures; Python also executes the original
function body to verify them. `MEMORY_PRIVACY_SECRET` is a dedicated stable key
shared by Core and Jobs, isolated from authentication and screenshot credentials.
The resource renderer rejects missing, split or reused references. Local test
targets generate one shared value. Neither the key nor receipt table appears in
user export, API memory objects or canonical intake operation payloads.

Migration 0174 adds the storage-only key and opaque 30-day receipt table. A
receipt may seal an already content-free tombstone. During its lifetime D1
rejects updates to that identity and its recreation after physical removal,
including MCP's content-derived-ID upsert. An old writer lacking a key is
rejected for an account with a live receipt. Fresh current writers provide a
key; unrelated existing legacy rows remain editable without a bulk backfill.
Existing populated keys and uid/item identities cannot be changed. Jobs expires
receipts on its existing schedule; account erasure and its write fences include
the table.

The preceding memory table prohibited null content. The migration rebuilds it
to admit the original upstream `content=None` shape only with tombstoned item
and source states and a deletion timestamp. Active rows still require 1–50,000
characters. The copy preserves row IDs without firing lifecycle or projection
triggers and restores all 16 existing dependent indexes/views/triggers unchanged.
No existing foreign key references this table. Tests preserve existing data,
projection work and paid-plan locks across the upgrade.

This is a prerequisite for canonical privacy apply, not a public DELETE
migration. The public handlers still require authoritative lineage/account/
destructive-operation admission, atomic scrubbing and receipt sealing, history
purging, observed provider cleanup and physical finalization. Other mutation
families still require canonical journal convergence. Route ownership and
production release status are unchanged.

## Verification

- The privacy suite passes 18 cases: upstream HMAC parity, absent/short-key
  denial without writes, legacy principals, scoped deletion, immutable identity,
  whole-batch rollback, all five public native/MCP/Developer creation shapes,
  deterministic MCP replay after physical erasure, conversation-extraction SQL,
  receipt expiry and a populated-schema upgrade.
- Three Worker tests cover TypeScript HMAC parity, invalid inputs and actual SQL
  expiry. Existing integration and X workflows verify persisted creator keys.
  The account-erasure schema inventory covers the new receipt table/fences.
- The existing route/inventory and manifest checks and TypeScript check pass.
  Final component runs: **992 Worker tests in 125 files, 850 Core tests and
  150 AI tests pass**. Core retains its existing Starlette/AnyIO deprecation
  warning. The initial full runs exposed omitted synthetic environment keys
  and the new table's account-write-fence registration; these were corrected
  without relaxing expected business behavior.
- Pinned Python formatting, diff whitespace, the installed local pre-commit
  hook and the zero-upstream-touch check pass. No default prompts change; the
  four protected provider/prompt files remain byte-identical to `9b7e48dca2`.

Commands use pinned Node 22 on PATH and the existing `uvx uv==0.12.3` runner:

```sh
bash deploy/cloudflare/ci/routes.sh
cd deploy/cloudflare
npm run typecheck
npm test
cd python/api-core
uvx uv==0.12.3 run pytest -q --tb=short
cd ../api-ai
uvx uv==0.12.3 run pytest -q --tb=short
```

Private logs under `$CODEX_HOME/eddy-production/` use prefix
`memory-privacy-receipts-`, with final `types`, `workers`, `core` and `ai` logs.
The full-lane logs retain the initial failures; the final component logs record
the corrected reruns.

## Actual hosted D1 upgrade

The owned `memory-privacy-receipts-hosted-20260907-c` run applies the preceding
176 App migrations to a fresh isolated database, writes three synthetic legacy
rows and then applies migration 0174 through ordinary remote Wrangler migrations.
All prior rows, three projection tasks and 723 existing schema dependencies
remain identical. Six rejected SQL writes return explicit CHECK/receipt errors:
invalid active content, late update, identity/key replacement and replay both
with and without the receipt key. Unrelated same-account and other-account rows
remain writable and a fresh keyed memory is admitted. The synthetic tombstone
and receipt setup is explicit test input, not a public deletion result.

The first remote migration form returned `incomplete input`; the current
parenthesized CASE form passes. A second run stopped during prior-schema setup
with an authorization error (7403) while using a static OAuth token. The final run uses Wrangler's normal OAuth
credential renewal for CLI commands. No error or observation timeout is counted
as an expected SQL rejection; the final run checks the actual returned reason.
All three owned test databases were observed absent after their respective runs.

The successful run finished at `2026-09-07T05:04:53.617Z`. Its uploaded migration
SHA-256 matches the current file:
`8eb29a8545e38cceaadb952a1022126af2dd273985c1aee8d8e21f07e2925294`.
Journal SHA-256:
`1ba1f9729c01b580f5d140eaff932202f7e26f623c171bd2b94fa5f6c1822ec1`.
Source and result hashes are in the private
`memory-privacy-receipts-evidence-20260907.json`.

## Public local business execution and remaining uncertainty

The fresh `memory-privacy-receipts-local-20260907-b` target passes all **16 Core**
and **18 recording/privacy** cases through actual application Workers, D1, R2,
Durable Objects and Queues. It verifies capture/finalization, memory intake and
editing, export, cascade deletion and complete account erasure. Inference and
Vectorize IO are controlled; it does not establish hosted-model behavior or
complete canonical privacy deletion.

The initial local A run returned one batch-intake HTTP 503. A CPython replay
against a copy of the retained database passed. The first runtime diagnostic
attempt did not execute because the trace already existed. A second diagnostic
with a fresh trace executed all 16 Core cases successfully, followed by the
clean B target's complete pass. The original 503 has not been explained and
remains a production-qualification investigation item; passing reruns do not
establish that it was fixed. Diagnostic changes existed only in the private
synthetic fixture and were restored afterwards.

The private production inventory/key-map successor files are
`inventory-production-20260907-privacy.json` and
`secrets-production-20260907-privacy.json` (15 isolated references, files mode
0600). They retain the dedicated key for the next fresh release candidate.
No production Worker was deployed, no production schema was migrated, and the
already signed macOS artifact was not rebuilt for this backend change.
