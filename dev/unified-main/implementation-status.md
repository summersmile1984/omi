# Three-track delivery status

The delivery target is one `main`, two deployment targets, and white-label
clients. The [2026-09-04 audit](audit-2026-09-04/01-self-host-action-plan.md)
defines the acceptance criteria. A local implementation or unit-test pass is
not a release, a signed client, or a completed product loop.

Candidate branch: `codex/unified-delivery`, integrated through `0227847d28`, starting at `b9776fac12f6`
(audit documents on `origin/main` `d238a85af9d9`). Nothing in this candidate
has been pushed, merged, or deployed to production.

The latest read-only remote check still found fork `main` at `d238a85af9d9`.
Upstream advanced to `176a02fe0d95` (`v0.12.286`); 386 upstream commits remain
outside this candidate. A merge-tree probe found three conflicts: `Makefile`,
`backend/testing/desktop_beta_admission/run.sh`, and
`backend/utils/llm/model_config.py`. It changed no checkout or main branch.
This snapshot is saved under `/tmp/memweft-implementation/ci/upstream-current-*`.
The previous 329-commit count applied to upstream `c70152426f22`, not this newer
version. Upstream integration and its full rerun remain acceptance work.

| Package | Owner | Status / next evidence |
| --- | --- | --- |
| SH-1 startup, profile, migration | Server | Integrated `3eaad62568`; actual amd64 API/4 consumers, fresh/repeat/v2-upgrade PG, fatal bad-config/consumer tests passed |
| AUTH-1 shared identity | Integration | Integrated through `0cbdc4a32b`: shared JWT/live sessions/password policy, PG password upgrade, selected identity deletion, encoded imported UIDs and canonical import ordering. Real PG/workerd/browser checks, separate ARM64/AMD64 Auth images, deletion lost-response/retry/zero-residual proof and old-image import failure/new-image reconciliation passed. macOS and Android consumers are integrated; bounded JWT cache skew is shared by Web/macOS (`a5f95a8e85`). OAuth and complete product-family erasure remain |
| WL-1 brand input/render/check | White-label | Implemented and integrated as `0bb1c37ce9`; 35 brand + 7 profile tests and actual target CLI fixtures passed |
| CF-1 route inventory | Cloudflare | Integrated `526fae377e`; CF-2 corrected live-model ownership: 612 backend identities = 576 staging-owned + 36 blocked. Desktop prompts passed real workerd/D1 |
| SH-2 storage and retrieval | Server | Integrated through `822b9d4163` plus AUTH deletion `2965b29656`/`87417d7a5e`: PG completion receipt, authoritative MinIO/Qdrant adapters, five captured provider fences and exclusive wipe lease. Real standard amd64 PG/Qdrant/MinIO/Redis evidence passed. Direct PG/background writes and stale snapshots are fenced (`b7a1939005`), with actual PostgreSQL lock/serialization tests. Schema v5 admits the dynamic onboarding owner (`91d5e1ca9a`), preserves existing data and rejects older images; unverified external/non-UID families remain |
| SH-3 providers and egress | Server | Integrated `683aa04d0e`/`5c8cc253da`: one BGE-M3 model/artifact/dimension contract, real CPU Ollama embeddings, bound Qdrant metadata and Chinese retrieval, Tasks persistence and explicit disabled STT/TTS/push. Real HTTP/WS and synchronous/asynchronous BYOK notification paths passed with zero FCM/token/cooldown effects. Pinned local SenseVoice/Kokoro speech is integrated (`c8f13c5e35`): actual CPU PCM/multipart/TTS/PTT/listen passed. Terminal usage settles accepted audio once (`ab47ce1e68`), and text/empty keepalives cannot extend the audio-idle deadline (`4719476f77`); real Redis usage and 30-second idle evidence passed. Full local LLM, finalization and conversation/memory loop remain |
| INTEGRATION-1: CLIENT-1 Web + CF-2 + WEB-1 | All | Integrated Web `8404f58a4d`, dual builder `a0b600883b`, request ownership `30998638b2`, capabilities `9de1dd5d26`/`b90e63de5c`, CF realtime `cf5d5fe308`. Both targets passed real browser signup/restore/logout and Tasks create/reload/complete; cross-user denial passed. Both API/Core restart checks retained task state and actual browser ASR transport passed with synthetic provider. Full conversation/memory/provider loop and error-shape parity remain |
| CLIENT-1 native consumers | White-label | macOS integrated `44d1fb7bd7`/`556dfc2f8c`: full app compile and actual named-bundle login, Keychain restore, protected JWT calls and logout/revocation passed against both targets; CF header WS used a synthetic ASR fixture. Android consumer is integrated (`44327be458`): both target debug APKs, real login/cold restore/refresh/Tasks/logout, rejected-password feedback and delayed owner/name/cleanup races passed; the CF name form was exercised offline and after recovery. iOS and Windows/Linux consumers remain |
| CF Python runtime | Cloudflare | Integrated `a1f4ae7733`: pinned workers-py/uv entry used by local startup and both publishers; 798 Worker tests, Core398, AI118 and seven dry-runs passed. Actual Core restart preserved task state. Native workerd launcher repair `1534fab379` preserves the runtime readiness descriptor; root independently passed its seven tests. Fresh tool installation remains registry-cache/TLS limited; version-checked installed runtime was exercised |
| CF-3 resource manifests | Cloudflare | Integrated `69929dcc98`/`701daa225d`: brand/stage/account input produces eight Worker configs and 28 resource entries, with migration/binding/secret-reference checks and output-ownership guards. Two real 26-route Web builds, 16 Worker dry-runs, four local D1 databases and brand isolation passed. Remote state remains unverified; release readiness is false |
| CF-4 pure/hybrid ownership | Cloudflare | In progress: bind real Realtime sessions/transcripts to D1 and existing Queue finalization, with reconnect/deletion fences. Isolated workerd/D1/DO/Queue runner is being exercised; remaining upstream-route ownership is not qualified |
| CI-1 same product contracts | Integration | Shared AUTH/Web/profile/provider/resource guards run in both manifest lanes. Manual diff-base `efc2282a05` and macOS native/compile lane `023fe3234d` are integrated. Common public identity/onboarding/Tasks suite and isolated Server runner are integrated `0227847d28`: eight actual HTTP cases, three process/output ownership tests, 12 upstream and three fork checks passed. The first CF run correctly failed 400/422 divergence; typed Tasks repair `1272c0e2c1` restored all eight common cases on the actual local CF target. Its formal isolated runner is still being completed. Earlier combined tree `f3e91e6dc4` passed 29 upstream and 11 fork checks, including both Flutter bundles and actual macOS compile (96.63s). No remote Actions or complete release qualification is claimed |
| WL-2 macOS identity | White-label | Isolated stage guards cover bundle storage namespace and disabled updater; complete signing/updater/package and supported-OS verification remain |
| WL-3 mobile identity | White-label | Android local debug package/profile/secure-storage staging is integrated `44327be458` with pinned Flutter3.44.5/Dart3.12.2. Real two-target native auth and CF name-error/recovery flows passed. The release task deliberately rejects this debug-only stage; iOS extensions, signing, push/OAuth, complete assets and distribution identity remain |
| WL-4 Windows/Linux identity | White-label | In progress on fixed `44327be458`: Electron main process owns opaque sessions and profile-derived API/JWT relay; secure-storage, teardown and package isolation are being implemented. Actual Windows DPAPI/installer and Linux keyring/runtime qualification remain |
| WL-5 text/assets | White-label | Pending built-product branding sweep and runtime surfaces |
| WL-6 device/OTA identity | White-label | Pending BLE/DIS/NFC/model/OTA implementation and hardware evidence |
| WL-7 artifact matrix | White-label | Pending brand × target × platform build and release verification |
| SH-4 Linux operations/release | Server | Stage-owned Auth launcher and dependency-cache/source-label package integrated `85e6e8ebc1`; local startup/migrate and beta/prod rejection passed. Runtime context cache leakage is repaired (`96380cfcb8`): both actual Docker context filters and a standard amd64 image exclude local validation state. Full Linux install/upgrade/restore/model acceptance and candidate manifest remain |
| CF-5 staging release | Cloudflare | Local frozen candidate tools are integrated (`bc64267eb9`/`ff3610160d`): Moonshine dual builds, eight Worker payloads, hashed source/assets, ordered resource/version journal, bounded subprocess ownership and conservative recovery. Actual local prepare/check/build/dry-runs passed. Apply/restore refuse missing CF-4/CI-1/prior-schema proof; no remote resource creation, secrets, migrations or publishing has been executed |

Engineering fixtures use synthetic brand names and temporary local data. Actual
distribution identities, signing/OTA keys and hardware results must be recorded
when available; they cannot be inferred from fixture builds. Access-control,
migration and release changes require explicit authorization before merging or
remote deployment under repository rules.

Recent evidence: [dual-target Web Tasks](implementation-2026-09-04/WEB-tasks-verification.md), [PG auth migration](implementation-2026-09-04/AUTH1-pg-migration-verification.md), [selected identity deletion](implementation-2026-09-04/AUTH-identity-deletion-verification.md), [import ordering](implementation-2026-09-04/AUTH-import-order-verification.md), [provider erasure](implementation-2026-09-04/SH2-providers-verification.md), [real model/runtime](implementation-2026-09-04/SH3-model-verification.md), [stage-owned startup](implementation-2026-09-04/SH4-startup-verification.md), [CF realtime](11-cf2-realtime-evidence.md), [CF Python runtime](12-cf-runtime-evidence.md), [CF resources](13-cf-resource-evidence.md), [CF frozen release](14-cf-release-evidence.md), [PG onboarding v5](implementation-2026-09-04/SH2-onboarding-schema-verification.md), [Docker context](implementation-2026-09-04/SH4-build-context-verification.md), [speech](implementation-2026-09-04/SH3-speech-verification.md), [PTT terminal usage](implementation-2026-09-04/SH3-ptt-usage-verification.md), [Android consumer](../../app/fork/VERIFICATION.md), [macOS consumer and limits](../../desktop/macos/fork/README.md).

Evidence is per package and exact local candidate tree. The Android/speech
combined check range is `96380cfcb8..f3e91e6dc4`; it is not a full cumulative
release-matrix run. Shallow-history recurrence checks explicitly reported SKIP.
The common-core candidate tree `bdbf05522abc51cedf8d29bd2f7a6fa640d96b7b`
passed its selected gates against `1534fab379`; see the
[core product evidence](implementation-2026-09-04/CI1-core-product-verification.md).
Stopped owned test services and removed compile-only caches are not deployed
products; logs and source records remain under `/tmp/memweft-implementation`.
Successful Tasks, identity and vector checks do not qualify every API, a complete
recording-to-memory loop, all white-label surfaces, signing or supported OSes.
