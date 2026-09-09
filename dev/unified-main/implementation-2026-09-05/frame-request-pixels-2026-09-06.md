# Cloudflare frame-request pixel ownership

Later verification found and repaired hosted multipart/codec failures. See the
[hosted runtime record](frame-image-hosted-runtime-2026-09-06.md) for the current
native Images implementation and boundary evidence. The implementation and
counts below describe this earlier commit.

Core now implements the four image routes alongside the four existing metadata
routes: multipart upload, promotion to conversation photos, temporary image
reading and private conversation photo reading. The entire frame-request family
remains blocked at Edge. Inventory stays **619 slots / 586 staging owners /
33 blocked**; this is internal implementation and local verification, not a
Cloudflare production release.

## Storage and publication

The ordinary Core builder stages the upstream image validation/canonicalization
functions from `backend/routers/frame_requests.py` without editing them. Their
JPEG/PNG/WebP inputs, 10 MiB file limit, 25-million-pixel decoder ceiling, EXIF
orientation handling, metadata removal and bounded JPEG output remain intact.
FastAPI uses `python-multipart==0.0.31`, matching the upstream backend pin, through
the existing Workers SDK runtime support. No new upload parser or prompt is used.

The temporary and permanent tiers have separate R2 bindings:
`FRAME_REQUESTS_TEMPORARY` and `FRAME_REQUESTS`. Neither uses the adjudicated
screenshot writer's namespace. Migration 0168 adds a durable object journal;
Core saves a multipart handle before uploading its first byte. Account/JIT
authority and the write lease are checked again during upload and publication.
Each copy has a unique physical object identity, so concurrent promotions cannot
overwrite each other's bytes.

One App D1 statement makes a ready object live and atomically updates the frame
request, conversation photo and content/photo markers. State, quota or photo-ID
conflicts roll back publication. A lost database response is reconciled against
the live reference; cleanup cannot erase a successfully published object merely
because its response was ambiguous. Concurrent promotions return the same
attached request and publish one photo. JSON state updates cannot invent image
storage: uploaded/attached requests must use the actual image endpoints.

Private reads recheck their owner/reference after the R2 fetch and before
releasing the response. Permanent conversation evidence survives JIT disable,
as upstream intends, while conversation/photo removal and account deletion
prevent further reads and retain durable physical cleanup work.

The scheduled Jobs reconciler and account deletion coordinator share
`frame-request-storage.ts`. Cleanup aborts the recorded multipart handle before
object deletion, preventing a late completion from recreating deleted bytes.
Failures retain the journal, retry time and original frame cleanup status.
Repeated account-deletion scans preserve backoff. Generic account D1 purging
cannot delete these receipts before both R2 prefixes and the journal are empty.

The existing resource renderer now plans **32 entries / 30 physical names**,
including two new frame buckets. Release policy requires seven-day expiry for
the temporary bucket and rejects automatic object expiry in the permanent
bucket. The existing namespace guard now discovers both upstream storage
owners, including their conditional bucket-name assignment. Release identity
and the existing route CI trigger also cover these original source dependencies.
The two new production buckets have **not** been provisioned. No production D1
migration, Worker publication or production resource mutation occurred here.

## Verification

- Full `env PATH=<pinned Node 22 bin>:$PATH bash deploy/cloudflare/ci/routes.sh`
  passed: inventory/manifest validation, TypeScript, **115 Worker files / 931
  tests**, **620 Core tests**, and **150 AI tests**. The final run also includes
  the account-deletion retry-backoff regression. Existing runtime deprecation
  warnings are not treated as failures.
- New Core behavior tests execute real HTTP handlers, Pillow and all App D1
  migrations with controllable R2 transport. They cover image normalization,
  owner/device isolation, promotion, photo deletion, overlapping promoters,
  ambiguous publication responses, revocation during work, malformed/oversized
  input and photo identity conflict. The six Jobs tests execute real SQLite
  migrations with a controlled R2 failure/late-completion seam, including the
  legacy principal without frame journals or bucket bindings.
- Two successful local native probes each executed **15 HTTP calls** through
  a fresh ordinary frozen Core build, workerd/Pyodide, migrated D1 and two real
  local R2 buckets. A private wrapper invokes the exact production Jobs cleanup
  function. Only synthetic account flags and a conversation were seeded; image
  state was created through the product handlers. Upload, unauthorized read,
  overlapping promotions, private photo reading, the original conversation
  photo list/delete endpoints and physical R2 cleanup passed.
- The first native input was a 1920-by-1080 RGBA PNG. Its **12,526-byte JPEG**
  matched the native upstream canonicalizer's bytes. A **1,772,828-byte**
  768-by-768 noise PNG also passed upload, storage, promotion, reading and
  deletion, producing **420,595 bytes** in workerd. Its readback digest matches
  the actual canonical digest recorded before upload; promotion retains those
  bytes unchanged.
- Pinned Black 26.5.1 and Prettier 2.8.8 were used on changed sources/ranges.
  Four protected prompt/tool files remain byte-identical to baseline
  `9b7e48dca2`. No upstream source or default prompt was edited.
  `git diff --check` and the whole-branch upstream-touch guard passed; the latter
  reports only the existing allowlisted desktop documentation change (+1/1).

## Limits and observations

The noise image's workerd JPEG does **not** match native CPython JPEG bytes.
Both decode as 768-by-768 RGB JPEGs with matching sampling and quantization
tables, but decoded pixel values also differ. Native Pillow 11.3.0 and 12.3.0
produced the same reference digest; the Workers Pillow 11.3.0 WASM output has a
different digest. A runtime codec-build difference is a hypothesis, not a proven
root cause. Cross-runtime JPEG byte identity and identical decoded pixels are
not claimed. The upstream canonicalization code was not changed to force a match.

An earlier large-input probe reached successful upload/read responses, then
spent excessive host memory formatting a failed Node Buffer deep-equality
assertion. Its verified private probe process was terminated; it is not counted
as passing or as a Worker memory-limit measurement. The corrected probe uses
bounded digests and preserves the observed cross-runtime mismatch. The initial
native build also stopped when dependency synchronization added upload-time
metadata for the new multipart wheel; the reviewed lock metadata is now checked
in and subsequent ordinary builds completed without lock changes.

These probes do not qualify the full 25-million-pixel/10 MiB hosted envelope,
hosted CPU/memory behavior, Better Auth login, public Edge, real AI quality or
production deployment. The existing Cloudflare product/dual-target release
qualification requirements remain outstanding; route inventory is unchanged.

Private evidence under `/Users/macstudio/.codex/eddy-production/`:
`frame-pixels-routes-final-20260906.log`,
`frame-pixels-native-20260906-{b,d}/result.json`, the corresponding runtime/build
logs and image fixtures, and `frame-pixels-prompt-integrity-20260906.json`.
Native probes use synthetic internal assertions; provider credentials are not
embedded in source or these probe fixtures.
