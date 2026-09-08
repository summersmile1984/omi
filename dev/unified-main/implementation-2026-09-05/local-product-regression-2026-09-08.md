# Local Wrangler and dual-target regression

The actual source-mode Wrangler run passed 49 cases: 16 shared HTTP, 20
recording/privacy, 11 chat and two share. The standard Docker Server run passed
the same 16 common cases. The Docker credential helper stalled while resolving
a public Python image; a task-local anonymous Docker config with the existing
socket and Compose plugin paths allowed the normal build to complete. The host's
global credentials and other running containers were not changed.

`contracts/deployment/regress.mjs` now runs all four existing Cloudflare suites
against a verified frozen candidate and the Server core against that candidate's
verified source. It rejects missing/failing/wrong-brand reports and mismatched
common case IDs, verifies candidate bytes around execution and awaits both
owned teardowns on failure. It uses the existing LocalProcesses boundary. Reports
list their exact candidate, brand and cases with `release_qualified: false`.
The fixed `release.mjs prepare` CLI invokes this implementation after freezing,
so a local product failure fails the existing release lane. There is no new
operator approval file or substitute complete-product attestation.

The existing Workers local/CI suite discovers the new composition tests:
1068 tests passed in 130 files (19.51 seconds); TypeScript also passed. The
existing source-mode product lanes remain registered in both local and CI.
`npm run test:product` and `npm run dev:product -- --output /absolute/new-target`
expose those existing actual Wrangler commands. Source fixtures use synthetic
Local Atlas presentation; frozen mode preserves the real candidate brand.

## Clock-dependent test failure found by prepare

Candidate prepare `candidate-production-20260908-local-regression-a` failed the
complete Core suite: 1271 passed and 21 JIT cases failed after 707.31 seconds.
The fixture froze reservations at 2026-09-08T08:00:00Z, but native memory capture
and the staged canonical apply kernel still used the real clock. After that
instant, newly created triggers had future `valid_from` values relative to the
fixture's reservation. The original paid-work policy correctly denied them.

The fixture now controls both capture and canonical apply clocks alongside its
reservation clock. The added before/equal/after cases execute the production
ASGI reservation and SQL paths and retain the real future-trigger 409. This
follows `backend/models/jit_proactivity.py:is_jit_trigger_paid_authority` and
`backend/models/memory_apply.py:_materialize_memory_item` time semantics; no application code,
default model prompt, error status or authority gate changes. Existing feedback
tests reuse the same fixture. All 45 focused cases pass in 35.45 seconds. The failing prepare log remains retained instead
of being replaced by an older passing report.

## Commands and evidence

```sh
npm --prefix deploy/cloudflare run test:product
PYTHON=backend/.venv/bin/python bash deploy/self-host/ci/product.sh
node contracts/deployment/regress.mjs --candidate /absolute/candidate
PYTHONDONTWRITEBYTECODE=1 deploy/cloudflare/python/api-core/.venv/bin/python -m pytest deploy/cloudflare/python/api-core/tests/test_jit_proactivity_reservations.py deploy/cloudflare/python/api-core/tests/test_jit_trigger_feedback_routes.py -q
```

Source reports are retained at
`/var/folders/v5/vwp83hn54v75tx3l7kqjdp7w0000gn/T/memweft-cloudflare-product.MsssAy/target/trace/`
and
`/var/folders/v5/vwp83hn54v75tx3l7kqjdp7w0000gn/T/memweft-product-contract.7vjeSr/server/core-results.json`.
The current frozen run is
`/Users/macstudio/.codex/eddy-production/candidate-production-20260908-local-regression-c`.
Its combined report is stored outside the immutable candidate at the
`product_report` path printed by a successful prepare; absence is not a pass. Its commands also retain individual
private target reports. The separate interactive environment uses
`/private/tmp/eddy-wrangler-dev-20260908-a/metadata.json`; it has a one-hour lifetime.

Inference/ASR and transient vector IO remain controlled in these local product
suites. Remote provider behavior, complete CF-4/CI-1 qualification and production
Worker deployment are not established by this change. It retains the existing
complete release admission contracts.

Prepare attempt b was deliberately interrupted before completion after review
found that the new CLI placed its report inside the already hashed `logs/` tree.
The CLI now writes to its own fresh private report directory and re-verifies the
immutable candidate afterwards. An interrupted test run is not counted as a pass.
