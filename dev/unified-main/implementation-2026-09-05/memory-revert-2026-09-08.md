# Explicit canonical ledger restoration on Cloudflare

The actual Core composition and authenticated Edge now mount the original
`POST /v3/memories/{memory_id}/revert` contract. Upstream service methods, ledger
builders, deterministic row/evidence identity, slot registry, MemoryDB projector
and canonical apply rules are staged from source. Only persistence calls become
asynchronous. Default prompts are unchanged; this user action invokes no model.

A chain restore preserves the selected historical row, closes its current tail,
and appends a new fact. A standalone closed source keeps its historical state
and receives one source-keyed reopen receipt. Same UUID replay returns only its
still-current append; different operations cannot reopen a source twice. The
D1 participant checks the full observed lineage inside the canonical batch and
retains existing active-item mutation guards. New rows use the original CF keyed
privacy identity, allowing unrelated new writes after a deletion while refusing
re-creation of the deleted identity. Receipt export and both deletion owners are
included.

## Verification

Actual Core ASGI and all App migrations pass 18 focused scenarios, including both
restore paths, exact replay, two simultaneous submissions, different concurrent
UUIDs, source changes immediately before commit, privacy and locks, final-statement
rollback, source/replacement deletion and new restores after unrelated erasure.
The Edge owner/rate-limit, source identity and deletion-residual subset passes
24 tests. The full Core suite passed **1259 tests** (one existing Starlette
warning, 681.97 seconds); the final full Worker suite passed **1054 tests** in
129 files (18.54 seconds). TypeScript and manifest checks passed. Core began
before the SQL CASE parenthesis correction below; its predicates are unchanged,
and the corrected frozen SQL is covered by transport tests and the hosted run.

The first full Worker run passed 1053 cases and caught the new migration's bare
`SELECT CASE` syntax. Parenthesized CASE is required by the existing remote D1
migration-transport contract; the migration was corrected without changing its
SQL conditions. Its three parser/transport tests pass. Local SQLite behavior
alone did not prove that transport.

A real hosted run, `eddy-memory-revert-20260908-c`, passed **32 public HTTP
checks**, finishing at `2026-09-08T06:13:21.703Z`. It used real Auth signup/session/
JWT, Rate Limit, Edge, Core, 191 App migrations and D1. It exercised actual append/
close, exact replay, four concurrent same-UUID requests, competing different UUIDs,
locked sources, account revocation and final-statement transaction rollback.
Historical rows were seeded after native intake; no model or native capture was
involved. Queue publication was real; this does not prove Vectorize delivery or
queued account erasure. Four Workers, two D1 databases and one Queue were deleted
with absence observed. The five runtime files, both generated modules and new
migration match that uploaded build byte for byte.

An earlier disposable run failed on the migration parser. A second encountered
Cloudflare authentication code 7403 during migration; its owned resources were
also fully cleaned. The successful driver supplies the existing credential to
Wrangler and may resume the same frozen migration set after this specific auth
failure, first reading applied migration identities. No permission was widened.
An initial local output-directory collision occurred before any remote action.
Failed attempts and the passed run are archived separately, excluding secret files.

Evidence: `/Users/macstudio/.codex/eddy-production/memory-revert-public-20260908/`.
The last production inventory read, `2026-09-08T06:07:15.540Z`, found all nine Eddy
production Workers absent. The successful run above used disposable verification
names and is not an Eddy production release.

The 17 blocked route-family identities retain their classification. Native
ledger production, remaining JIT projections, two-target qualification, CF-4/CI-1
release executors and Eddy production publication remain incomplete.
