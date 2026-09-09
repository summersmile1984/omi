# Cloudflare screenshot storage prerequisite

Starting revision: `03962a26eb`. This change adds an independent screenshot
storage Worker; it does not classify the eight public screenshot routes as
implemented. The inventory remains 619 slots, 586 staging-owned and 33 blocked.
Core adjudication, approval minting, survivor selection/publication, public URL
proxy and hosted business qualification remain required. No default prompt or
upstream-owned file is changed.

## Storage and deployment ownership

`screen-frame-writer` alone holds `SCREEN_FRAMES` R2 and App D1. Core and Jobs
receive its service binding. Core and the writer share a dedicated screenshot
signing credential; the writer refuses reuse of the common internal assertion
key. Resource rendering enforces bucket/service/AI isolation and matching
signing references. Desired topology is now nine Workers, six R2 buckets and
30 total derived resources. This change makes no remote resource or deployment mutation; Eddy needs a
fresh allocation and candidate for the expanded topology.

Migration 0166 owns legacy-default-enabled settings, survivor sets, attempts
and one-use write receipts. Receipt identity, bytes and metadata are bound to
the signed approval. Each multipart upload ID is durably admitted before any
image part is sent. Erasure fences the receipt, aborts uploads, deletes objects
and only then removes the receipt. Expired ordinary receipts retain replay
protection until cleanup; account fences independently reject late admissions.

An R2 abort acknowledgement does not erase an already-completed object.
Documented NoSuchUpload code 10024 is terminal for the upload; other provider
errors retain cleanup state. Revocable content capabilities recheck D1 sharing,
membership and account state after fetching R2. Jobs confirms writer erasure
before product D1 purge and Auth deletion, and includes it in later zero scans.
The generic D1 purge list cannot remove writer receipts. Export includes owned
screenshot settings and sets, without internal upload/attempt journals.

Implementation and protocol references are in the
[writer guide](../../../deploy/cloudflare/workers/screen-frame-writer/README.md).

## Verification

- `bash deploy/cloudflare/ci/routes.sh` with the pinned Node 22 path: six route
  inventory tests, 619 registered slots, manifest validation and typecheck pass;
  **114 Worker test files / 922 tests**, **550 Core tests**, **150 AI tests** pass.
  Core reports its existing Starlette deprecation warning.
- The writer's behavioral tests execute production approval, storage and HTTP
  handlers with all actual App SQL migrations. They cover byte mismatch,
  replay, invalid/expired claims, settings/subject/epoch changes, four upload
  erasure races, cleanup retry, credential separation, current sharing and
  uid-bound internal requests. Existing account deletion tests also prove that
  a writer outage retains the fence and identity.
- `screen-frame-r2.test.mjs` executes actual workerd over local HTTP. Its first
  case proves R2 abort/complete/delete behavior. Its second applies all App D1
  migrations and bundles the production writer behind fixture-only SQL setup:
  write **201**, read exact approved fixture bytes **200**, account-fenced
  read **404**, then **zero R2 objects and zero receipts**. This does not run
  the privacy model or prove that arbitrary image content was adjudicated.
- Normal locked Wrangler `deploy --dry-run` builds the writer successfully;
  output binds only App D1 and `SCREEN_FRAMES`. No remote deploy occurs.
- The existing actual application product command (`contracts/local-target.mjs`
  with `--run-core --run-recording --run-chat --run-share`) builds the expanded
  stack, applies SQL and passes **15 core, 18 recording, 11 chat and 2 sharing
  cases**. Actual public HTTP/WS and Queue deletion are exercised. Both erased
  accounts reach zero product/Auth/R2 residuals with one expected short-lived
  tombstone each; the unrelated survivor remains. Inference is controlled in
  this hermetic lane and hosted Vectorize is not exercised. Runtime logs include
  failed/retried Queue deliveries before the successful final residual checks;
  this is not a claim of an error-free hosted soak.
- Fixed Python/Prettier formatting, `git diff --check`, no upstream-file touches
  and byte equality of the four protected prompt/tool files against
  `9b7e48dca2` are verified.

Private evidence is under `/Users/macstudio/.codex/eddy-production/`:
`screen-frame-full-cloudflare-20260906-i.log`,
`screen-frame-writer-build-20260906-i.log`,
`screen-frame-writer-build-20260906-i/`, and
`screen-frame-product-20260906-i/trace/`. The standalone native HTTP diagnosis
is recorded in `screen-frame-native-http-20260906-h.log`; the final full suite
also runs it with isolated signing/assertion credentials. No keys or private
account traces are committed.

The next implementation boundary is Core's unchanged upstream adjudication
flow and all eight public screenshot routes. Hosted R2 races, Pyodide memory
limits and actual model access still need qualification. CF-4/CI-1 release
executors and the remaining route/lifecycle work remain open; this commit is
not proof of Eddy production deployment or macOS-to-production acceptance.
