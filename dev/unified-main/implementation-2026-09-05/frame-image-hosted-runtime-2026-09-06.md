# Eddy frame images: hosted runtime boundary repair

Verified on 2026-09-06 at 08:53 UTC against the actual Cloudflare-hosted Python
Core and native Images binding. This supersedes the unresolved frame-upload
runtime observations in the earlier [pixel](frame-request-pixels-2026-09-06.md)
and [provisioning](frame-resource-provisioning-2026-09-06.md) records. It does not
qualify the separate screenshot-adjudication pipeline or the complete product.

## Failure and repair

The original hosted Pillow path accepted a small image but failed on supported
inputs. A cold first 10 MiB multipart request returned HTTP 400; a diagnostic
exception handler identified Starlette's temporary-file spool as `OSError`,
`[Errno 8] Bad file descriptor`. A 25-million-pixel RGBA PNG returned HTTP 500
with Cloudflare resource-limit code 1102. That code does not distinguish CPU
from memory exhaustion by itself. Prior remote-preview 503/1105 responses were
startup observations; an actual tagged deployment subsequently started the full
Core and returned health 200. They were not evidence of image memory exhaustion.

Frame uploads now bound the whole multipart stream to 10 MiB plus 64 KiB of
form overhead and keep it below Starlette's spool threshold. The existing file
limit stays 10 MiB, with one file and bounded fields. Interrupted parsing closes
its temporary objects. Missing file input retains FastAPI's 422 behavior.

Full-resolution decoding, transformation and JPEG encoding now belong to the
required native `IMAGES` binding. Core reads container metadata without loading
the pixel plane, removes private metadata before provider IO, applies the
upstream EXIF orientation explicitly, and validates and strips the resulting
JPEG again. The provider's own JPEG output preserved EXIF in an earlier hosted
probe, and WebP orientation was ignored; treating an HTTP 200 as semantic success
would have missed both defects. The output keeps the existing white alpha
background, JPEG quality 85, 1920-pixel longest edge and 2.5-million-pixel ceiling.
Native provider failure returns 503 and leaves the upload claim retryable.

WebP preparation deliberately avoids Pillow `Image.open`: libwebp's animation
decoder allocates two full RGBA canvases at creation, even before frame loading.
RIFF metadata is parsed without those buffers, while native `Images.info` checks
the WebP bitstream and dimensions. PNG metadata parsing skips compressed pixel
chunks rather than calling PNG's pixel-loading `getexif` override. JPEG scan
parsing removes metadata between progressive scans and before EOI too.

This changes the Cloudflare codec owner, not the Server OS implementation or
default prompts. Exact JPEG-byte equivalence across different codecs is not
claimed. R2 still stores the final canonical bytes; reads stream the same bytes
after ownership, expiry and deletion checks, without another transformation.
No direct-R2 signed-link migration was made.

## Executed verification

- `env PATH=<pinned Node 22 bin>:$PATH bash deploy/cloudflare/ci/routes.sh`
  exited zero: route inventory and manifest validation, TypeScript, **115 Worker
  files / 932 tests**, **653 Core tests** and **150 AI tests** passed. Core retains
  one existing Starlette/AnyIO deprecation warning. The production tests cover
  three formats and all eight EXIF orientations, private provider metadata,
  progressive JPEG metadata, no source-sized PNG/WebP decode, native outage and
  retry, actual HTTP multipart with an intentionally failing file rollover, and
  R2 publication/ownership/deletion behavior through controlled service seams.
- The private diagnostic deployed the full frozen Core plus the four current
  production modules and an authenticated synthetic-image endpoint. It used the
  native Images binding, no D1/R2 or AI-provider bindings, and no user content.
  All **14 image cases** passed through actual hosted HTTP: first cold 10 MiB
  upload 200; one byte over 413; 25-million-pixel PNG, WebP and JPEG 200;
  over-limit PNG/WebP 413; ordinary images after errors 200; JPEG/WebP EXIF
  rotation, PNG transpose orientations 5/7 and alpha PNG 200.
- The returned boundary images are 1581 × 1581 JPEGs. Oriented images have the
  expected 200 × 400 dimensions; alpha output is 100 × 200. Independent local
  `ImageOps.exif_transpose` and white compositing comparisons found maximum
  sampled corner-channel errors of 0–1. Every successful returned image has
  empty EXIF, no synthetic private marker, and satisfies both output-size limits.
  The response digests match the exact saved canonical bytes.
- Hosted source SHA-256 values for `frame_image_metadata.py`,
  `frame_image_transform.py`, `frame_upload_form.py` and `frame_request_routes.py`
  match the current worktree. All four protected prompt/tool files remain
  byte-identical to baseline `9b7e48dca2`.
- Diagnostic Worker `eddy-images-probe-20260906-g` was absent before creation.
  Its exact version `52a49906-1242-4a71-8aeb-0d2cb775d951` and private transaction
  tag were reobserved before owned deletion; final readback confirmed absence.

The live run exercises the actual upload parser and image helpers, not public
Edge admission, authenticated R2 business persistence or the native desktop.
The R2 regression suite uses controlled transports. No production Worker was
published or business SQL applied. The 619-slot route inventory remains at
586 staging owners / 33 blocked; CF-4, CI-1 and production macOS business
acceptance remain unfinished. The earlier frame-b release candidate is stale
after this source change and must not be published as the repaired version.

Private evidence in `/Users/macstudio/.codex/eddy-production/`:
`frame-image-routes-20260906{.log,-result.json}` and
`frame-images-binding-20260906-g/{scope.json,hosted-result.json,semantic-verification.json,case-*.jpg}`.
The failure-class record is `FC-cloudflare-image-runtime-envelope`; these
behavioral regressions run in the existing Cloudflare route local/CI lane.

Primary protocol references: [Images binding](https://developers.cloudflare.com/images/optimization/binding/),
[Worker limits](https://developers.cloudflare.com/workers/platform/limits/),
[WebP container](https://developers.google.com/speed/webp/docs/riff_container),
[libwebp allocation owner](https://github.com/webmproject/libwebp/blob/main/src/demux/anim_decode.c),
and [PNG specification](https://www.w3.org/TR/png-3/).
