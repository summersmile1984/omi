# Operator-owned mlx-audio MOSS diarization

This prerecorded-only provider is independent of `utils/moss_pipeline/`, which
owns the hosted `mosi.cn` API. Selecting `mlx_moss_diarize` never constructs the
hosted MOSS client and never falls back to it.

Required runtime configuration:

- `STT_PRERECORDED_MODEL=mlx_moss_diarize`
- `MLX_MOSS_DIARIZE_ENDPOINT`: exact `/v1/audio/transcriptions` URL. Public
  authorities require HTTPS; HTTP is accepted only for loopback/private/internal
  hosts. Metadata, link-local, multicast, reserved, unspecified, CGNAT, and legacy
  numeric-IP authorities plus `mosi.cn`/`omi.me` authorities are rejected before
  an HTTP client is constructed, regardless of scheme or API-key presence.
- `MLX_MOSS_DIARIZE_MODEL`: exact model id exposed by the operator's service.
- `MLX_MOSS_DIARIZE_API_KEY`: optional on private authorities; required for a
  public HTTPS authority and sent only as `Authorization: Bearer <value>`.

The adapter sends multipart `file`, `model`, `response_format=verbose_json`, and
bounded `max_tokens`. A well-formed single-language hint is sent as `language`;
`multi` keeps the model's automatic mode (the adapter does not claim a broader
language set than the mounted model). Up to 100 unique bounded keywords can be
sent as comma-separated `context`, which requires the operator's mlx-audio
hotword patch. The currently installed patch flattens diarized `verbose_json`
and removes `speaker_id` whenever context is present, so `diarize=True`
suppresses context before outbound I/O and emits shared `record_fallback`
telemetry (`capability_mismatch`, degraded); `diarize=False` sends context.
Invalid or oversized keyword sets fail closed instead of being discarded.
It accepts strict `segments[{start,end,speaker_id,text}]`, removes a matching
`[S01] ` text prefix, and ignores `total_time` because that is processing latency
rather than audio duration. `diarize=False` does not require a speaker model:
provider labels are flattened to `SPEAKER_00`, and a response without
`speaker_id` remains valid on that explicit nondiarized path.

Recordings are bounded and normalized to 16 kHz mono PCM WAV. Requests are
capped at four minutes (below the service's known >5 minute failure boundary)
and longer recordings are split with absolute timestamp offsets.
Speaker ids returned by MOSS are request-local, so recordings with multiple
chunks require `SPEAKER_EMBEDDING_PROVIDER=sherpa_onnx` and an explicit mounted
`SPEAKER_EMBEDDING_MODEL`. The adapter clusters per-chunk speaker clips at the
shared 0.45 cosine-distance threshold while preserving distinct speakers inside
each chunk; it fails closed when local embeddings cannot prove cross-chunk
identity. The provider does not download a model and has no default endpoint or
model.

`transcribe_url` downloads only from the exact configured private
`MINIO_ENDPOINT`/`MINIO_PUBLIC_ENDPOINT` origin, or from a DNS-pinned public
HTTPS authority. Loopback/private URLs not owned by that storage configuration,
link-local metadata, userinfo, redirects, and non-HTTP(S) schemes are rejected.
