# Audio merge staging boundary

The legacy `POST /v2/audio-merge-jobs/run` route remains legacy-owned. It
accepts the historical Cloud Tasks/OIDC payload and has provider and codec
semantics (including pydub/MP3) that are not reproduced by the Workers
runtime. This boundary must not be advertised as a cutover of that route.

## Staging contract

Workers exposes the explicitly namespaced routes below for validation:

- `POST /v2/cf/audio-merge-jobs/run` admits one conversation-level job.
- `GET /v2/cf/audio-merge-jobs/:jobId` returns the uid-scoped lifecycle.

Admission is Better Auth authenticated and checks the D1 conversation, lock,
recording-storage consent, current legacy audio metadata fingerprint, and the
account deletion generation. The request is bounded to JSON and accepts only
`audio_file_id=conversation` and `output_format=wav`. Unsupported formats and
file scopes fail closed.

`cf_audio_merge_jobs` is the D1 authority. Its `(uid, request_fingerprint)`
constraint makes repeated admission idempotent. A message on the existing
Jobs Queue claims a short D1 lease, verifies the deletion generation and
fingerprint, and invokes the existing R2 legacy-chunk rebuild. Retries are
bounded; terminal source errors are acknowledged and exposed as `failed`.
Playback objects are staged and committed through `cf_sync_playback_objects`,
and the conversation metadata is updated only after the generated WAV has
been written to the uid-scoped `sync-playback/` key.

The rebuild can consume the legacy R2 chunk families (`.batch.enc`,
`.batch.bin`, `.opus.enc`, `.enc`, `.opus`, and `.bin`) when the required
encryption secret and Opus decoder are available. It produces WAV artifacts;
it does not provide MP3 encoding, arbitrary codec conversion, historical GCS
object migration, or the original Cloud Tasks request/response compatibility.
Those gaps keep `/v2/audio-merge-jobs/run` on the legacy owner until a separate
contract and provider decision closes them.

Account deletion includes the D1 job surface and the source/artifact key
columns in its residual checks. The source and output prefixes are already
covered by the shared ASSETS deletion inventory (`chunks/{uid}/` and
`sync-playback/{uid}/`).

## Verification boundary

The contract is covered by Worker tests for Better Auth rejection, bounded and
unsupported-format admission, uid-scoped status/idempotency, and an R2 PCM
chunk rebuild through Queue processing. These tests validate the staging
boundary only; they do not claim parity for the legacy MP3/provider endpoint.
