# Three-track delivery status

The delivery target is one `main`, two deployment targets, and white-label
clients. The [2026-09-04 audit](audit-2026-09-04/01-self-host-action-plan.md)
defines the acceptance criteria. A local implementation or unit-test pass is
not a release, a signed client, or a completed product loop.

Candidate branch: `codex/unified-delivery`, starting at `b9776fac12f6`
(audit documents on `origin/main` `d238a85af9d9`). Nothing in this candidate
has been pushed, merged, or deployed to production.

| Package | Owner | Status / next evidence |
| --- | --- | --- |
| SH-1 startup, profile, migration | Server | Integrated `3eaad62568`; actual amd64 API/4 consumers, fresh/repeat/v2-upgrade PG, fatal bad-config/consumer tests passed |
| AUTH-1 shared identity | Integration | Integrated through `1be55bf3bd`: shared claims/live sessions/CORS/password policy, HTTP503/WS1013, cookie preservation, PG native password upgrade. Actual PG/workerd/browser auth passed; fresh Firebase import and legacy JWKS migration/grace/revocation passed on new Linux ARM64 Auth image. AMD64 Auth npm build failed; native consumers/OAuth/complete identity deletion remain |
| WL-1 brand input/render/check | White-label | Implemented and integrated as `0bb1c37ce9`; 35 brand + 7 profile tests and actual target CLI fixtures passed |
| CF-1 route inventory | Cloudflare | Integrated `526fae377e`; CF-2 corrected live-model ownership: 612 backend identities = 576 staging-owned + 36 blocked. Desktop prompts passed real workerd/D1 |
| SH-2 storage and retrieval | Server | PG erasure/receipt owner integrated `3cddcf6a19`; actual PG25 tests, JWT/API deletion gate and standard amd64 Python image passed. Receipt-aware provider write fencing, MinIO/Qdrant/Redis product loop remain; HTTP denial alone does not prove background write fencing |
| SH-3 providers and egress | Server | Pending; real selected STT and disabled-provider boundaries required |
| INTEGRATION-1: CLIENT-1 Web + CF-2 + WEB-1 | All | Integrated Web `8404f58a4d`, dual builder `a0b600883b`, request ownership `30998638b2`, capabilities `9de1dd5d26`/`b90e63de5c`, CF realtime `cf5d5fe308`. Both targets passed real browser signup/restore/logout and Tasks create/reload/complete; cross-user denial passed. Both API/Core restart checks retained task state and actual browser ASR transport passed with synthetic provider. Full conversation/memory/provider loop and error-shape parity remain |
| CLIENT-1 native consumers | White-label | macOS package in progress: isolated full app compile, Swift auth core and compiler-based source stage tests passed; actual named-bundle login/Keychain/WS next. Other native platforms pending |
| CF-3 resource manifests | Cloudflare | Pending brand/resource binding/migration/rollback contract |
| CF-4 pure/hybrid ownership | Cloudflare | Pending explicit state owners and missing upstream-route implementations |
| CI-1 same product contracts | Integration | Shared AUTH, Web, route and fork guards run in both manifest lanes. Actual Compose/workerd/D1/DO product runners and native macOS lane remain. Tasks invalid-body status differs: Server422 / CF400 |
| WL-2 macOS identity | White-label | Isolated stage guards cover bundle storage namespace and disabled updater; complete signing/updater/package and supported-OS verification remain |
| WL-3 mobile identity | White-label | Pending iOS/Android IDs/extensions/callbacks/build verification |
| WL-4 Windows/Linux identity | White-label | Pending Electron installer/feed/update verification |
| WL-5 text/assets | White-label | Pending built-product branding sweep and runtime surfaces |
| WL-6 device/OTA identity | White-label | Pending BLE/DIS/NFC/model/OTA implementation and hardware evidence |
| WL-7 artifact matrix | White-label | Pending brand × target × platform build and release verification |
| SH-4 Linux operations/release | Server | Stage-owned Auth launcher and dependency-cache/source-label package integrated `85e6e8ebc1`; local startup/migrate and beta/prod rejection passed. Full Linux install/upgrade/restore/model acceptance and candidate manifest remain |
| CF-5 staging release | Cloudflare | Pending complete version-attributed candidate and live readiness/product evidence |

Engineering fixtures use synthetic brand names and temporary local data. Actual
distribution identities, signing/OTA keys and hardware results must be recorded
when available; they cannot be inferred from fixture builds. Access-control,
migration and release changes require explicit authorization before merging or
remote deployment under repository rules.

Recent evidence: [dual-target Web Tasks](implementation-2026-09-04/WEB-tasks-verification.md), [PG auth migration](implementation-2026-09-04/AUTH1-pg-migration-verification.md), [PG deletion](implementation-2026-09-04/SH2-verification.md), [stage-owned startup](implementation-2026-09-04/SH4-startup-verification.md), [CF realtime](11-cf2-realtime-evidence.md). Current successful local Tasks checks extend the earlier API-unavailable checkpoint; they do not retroactively qualify every API.
