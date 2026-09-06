# Native screenshot image processing — 2026-09-06

Screenshot adjudication now delegates full-size JPEG/PNG decoding, orientation
and resizing to Cloudflare Images. Core retains the original upstream final
canonicalizer: 1600-pixel longest edge, JPEG quality 82, 480-pixel thumbnail at
quality 75, and SHA-256 over the exact canonical JPEG. The privacy judge, approval
and isolated R2 writer still consume those exact bytes. Default prompts were not
edited. Preview reads still stream stored R2 objects after current permission
checks; they do not transform images or invoke a model.

`screen_frame_png.py` preserves the original discard-alpha behavior. It removes
alpha components directly from filtered PNG rows, since each PNG predictor uses
the same component of adjacent pixels. No source-sized pixel plane or Python
unfilter loop is needed. This covers RGB/gray alpha at 8/16 bits, all five filters
and all seven Adam7 passes. Palette transparency and private/color-profile
metadata are removed before Images. The output is checked and stripped before
the original encoder sees it. Animated/corrupt candidates and codec failures
remain per-candidate rejections and cannot authorize a model call or storage.
The route now requires an Images binding in its existing availability check.

Compressed feeds are limited to 64 KiB independently of PNG chunk size. Python's
`unconsumed_tail` copies bytes; feeding one 20 MiB IDAT repeatedly for every row
would create quadratic copying. A behavioral test uses a single large IDAT and
asserts the decoder's input bound while checking every resulting pixel. PNG
chunk enumeration also stays incremental instead of retaining a tuple per chunk.

## Executed evidence

The first hosted run exercised 19 cases. The next run,
`eddy-screen-image-20260906-b`, passed **25 native HTTP checks**: PNG/JPEG EXIF
orientations 2/5/6/7; Adam7 with 8/16-bit gray/RGB alpha; 64-million-pixel RGB and
RGBA; a 20 MiB single file; animated/corrupt rejection; eight small candidates;
the 20 MiB file in a 27,962,438-byte JSON request; four original-codec references;
and dense RGB/RGBA files of 17,306,005 and 19,699,788 bytes. The JSON probe uses
the actual upstream request model and digest verifier, then the production image
helper. It has no D1, R2, inference or account bindings and is not a public-route
or end-to-end product test.

Local pixel comparisons initially found a 32-channel-value difference on a tiny
high-frequency RGB16 fixture. Running the unchanged upstream encoder inside the
same hosted Python runtime resolved that observation: **all four Adam7 outputs
match the native upstream reference JPEG byte-for-byte**. The discrepancy is
between local and hosted JPEG encoders, not alpha removal or native Images.
Ordinary orientation, transparent-color and 64-megapixel fixtures retain the
expected dimensions and sampled colors, with no EXIF. Cross-runtime JPEG-byte
equivalence is not a requirement or a claimed result.

The B run finished at `2026-09-06T13:42:35.068Z`. Journal SHA-256:
`2d278fcbb38aaf6496c679db8c56eaf2f6b40b38e31c0c68a3f85c18feb534c3`.
Both A and B Workers were observed absent before creation. Their exact tags and
versions were rechecked before deletion; final readbacks confirmed absence.
The normal source builder produced fresh Core modules, then the diagnostic
replaced only the entrypoint and bindings. No stale candidate Core payload was
deployed. Diagnostics require a random private key and only use synthetic images.

The final run, `eddy-screen-image-20260906-c`, passes **28 native checks** against
the final source, including all B cases plus eight 64-megapixel RGBA candidates
in one JSON request, the 19,699,788-byte dense RGBA image in JSON, and the same
dense pixels packed into one 19,696,188-byte IDAT container. Their elapsed times
were approximately 29.0, 14.5 and 11.8 seconds respectively. All four original
native reference JPEGs still match exactly. Every deployed production image
module's hash matches the current source. The owned Worker was removed after
tag/version comparison; absence was confirmed at `2026-09-06T13:50:33.411Z`.
The run finished at `2026-09-06T13:50:33.412Z`. Journal SHA-256:
`cbd4f27ec1aa913aafed988c9e263a2e186376b91fe0e52706087d08f828d429`.

The existing `deploy/cloudflare/ci/routes.sh` lane passed inventory/manifest
validation, typecheck, **980 Worker tests in 123 files, 772 Core tests and 150 AI
tests**. After adding single-frame APNG and image-service-failure coverage, the
full Core suite passed **774 tests**. The final source including the bounded-IDAT
regression passed the complete **775-test Core suite**, with the one existing
Starlette/AnyIO deprecation warning. The 31-test image suite also passed on its
own. The component's ordinary test discovery owns
these tests; no separate unattended probe or CI lane was introduced.

## Remaining production work

This repair does not qualify the entire allowed eight-large-candidate request,
native model inference or the complete authenticated screenshot/D1/R2 flow.
The eight screenshot routes remain blocked; total inventory remains **602
staging-owned / 17 blocked**. The CF-4 product and CI-1 dual-target qualification
and the canonical memory/JIT work remain required. Do not publish the stale
release candidate as this repaired source.

A separate live observation at `2026-09-06T13:43:56.849Z` found all nine Eddy
production Worker names absent. Strict signature verification still passes for
`/private/tmp/eddy-macos-production-20260905-b/Eddy.app`; a live production desktop
login/capture/memory flow remains unverified. The full deployment goal is not met.

Private evidence under `$CODEX_HOME/eddy-production/` includes
`screen-image-hosted-20260906-{a,b,c}/result.json`,
`screen-image-hosted-20260906-c/native-reference-verification.json`,
`screen-image-cases-20260906/`, `screen-image-routes-20260906.log` and
`screen-image-core-final-c-20260906.log`.

Failure class: `FC-cloudflare-image-runtime-envelope`. The existing hosted frame
upload incident (Cloudflare 1102 at supported dimensions) identified the same
source-sized Pillow allocation boundary; the new tests exercise the screenshot
owner rather than asserting source strings. The provider seam, no-source-load
assertion, independent PNG filter encoder and actual hosted execution each cover
a different observable part of that failure.

Protocol sources: [Images binding](https://developers.cloudflare.com/images/optimization/binding/),
[PNG filters and Adam7](https://www.w3.org/TR/png-3/),
[Worker request and memory limits](https://developers.cloudflare.com/workers/platform/limits/).
