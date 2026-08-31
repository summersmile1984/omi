# Audio merge staging boundary

The staging `POST /v2/audio-merge-jobs/run` route is now owned by the Jobs
Worker. It accepts the historical schema-v1/schema-v2 JSON payloads and emits
MP3 through a pinned Worker-safe encoder. The Edge boundary requires a verified
Better Auth principal; historical Cloud Tasks/OIDC callers must be migrated to
that signed boundary before production use.

## Staging contract

Workers exposes the explicitly namespaced routes below for validation:

- `POST /v2/cf/audio-merge-jobs/run` admits one conversation-level job.
- `GET /v2/cf/audio-merge-jobs/:jobId` returns the uid-scoped lifecycle.
- `POST /v2/cf/audio-merge-jobs/legacy/run` admits the legacy payload shape
  under Better Auth and translates it to a durable Jobs Queue item.
- `GET /v2/cf/audio-merge-jobs/legacy/:jobId` returns that adapter's lifecycle.

The original `/v2/audio-merge-jobs/run` is an alias of the legacy adapter and
uses the same D1/Queue authority. It returns `202` with a queued job envelope;
the old Cloud Tasks dispatcher must be updated to consume the job result rather
than relying on the old inline `200 {status: done}` acknowledgement.

Admission is Better Auth authenticated and checks the D1 conversation, lock,
recording-storage consent, current legacy audio metadata fingerprint, and the
account deletion generation. The original legacy adapter accepts bounded JSON
schema-v1 per-file timestamps and schema-v2 conversation fingerprints and
produces MP3; unsupported fields, formats, and file scopes fail closed. The
separate `/v2/cf/audio-merge-jobs/run` contract remains WAV-only.

`cf_audio_merge_jobs` is the D1 authority. Its `(uid, request_fingerprint)`
constraint makes repeated admission idempotent. A message on the existing
Jobs Queue claims a short D1 lease, verifies the deletion generation and
fingerprint, and invokes the existing R2 legacy-chunk rebuild. Retries are
bounded; terminal source errors are acknowledged and exposed as `failed`.
Playback objects are staged and committed through `cf_sync_playback_objects`,
and the conversation metadata is updated only after the generated WAV has
been written to the uid-scoped `sync-playback/` key.

The WAV rebuild can consume the legacy R2 chunk families (`.batch.enc`,
`.batch.bin`, `.opus.enc`, `.enc`, `.opus`, and `.bin`) when the required
encryption secret and Opus decoder are available. It produces WAV artifacts;
the legacy alias below is the MP3 contract.

The legacy adapter closes the codec part of that contract for R2-backed staging
data: schema-v1 (per-file `timestamps`) and schema-v2 (conversation
`fingerprint`) payloads produce a single 48 kbps mono 16 kHz MP3 stream using
the pinned libmp3lame WASM encoder. It uses a separate D1 table,
uid-bound request fingerprints, a lease/retry state machine, and the same
`playback/{uid}/` R2 inventory used by the old reader. It does not claim
byte-for-byte ffmpeg parity or historical GCS backfill.

Account deletion includes the D1 job surface and the source/artifact key
columns in its residual checks. The source and output prefixes are already
covered by the shared ASSETS deletion inventory (`chunks/{uid}/` and
`sync-playback/{uid}/`).

## Verification boundary

The contract is covered by Worker tests for Better Auth rejection, bounded and
unsupported-format admission, uid-scoped status/idempotency, schema-v1 and
schema-v2 MP3 output, and missing-source terminalization through Queue
processing. These tests validate schema-v1/schema-v2 admission, MP3 output,
idempotency, retry/terminal behavior, uid isolation, and deletion residuals.
They do not claim Cloud Tasks OIDC or historical GCS cutover parity.
