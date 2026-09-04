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
| SH-1 startup, profile, migration | Server | Implementing isolated API/worker/bootstrap and fresh PG migration |
| AUTH-1 shared identity | Integration | Shared claims/session verification implemented; live PG + workerd login/refresh/logout passed. Key-grace/fault regression and consumer error mapping in progress; password/migration acceptance remains |
| WL-1 brand input/render/check | White-label | Implemented and integrated as `0bb1c37ce9`; 35 brand + 7 profile tests and actual target CLI fixtures passed |
| CF-1 route inventory | Cloudflare | Implemented in `67ac1f8949` awaiting integration; 612 routes inventoried, 35 CF-4 gaps recorded, desktop prompts passed real workerd/D1 |
| SH-2 storage and retrieval | Server | Pending SH-1; PG/MinIO/Redis/Qdrant product loop required |
| SH-3 providers and egress | Server | Pending; real selected STT and disabled-provider boundaries required |
| INTEGRATION-1: CLIENT-1 Web + CF-2 + WEB-1 | All | Pending shared auth/profile and executable backend; one candidate must pass both Web targets |
| CLIENT-1 native consumers | White-label | Pending platform profile/auth/revocation/realtime/capability integration |
| CF-3 resource manifests | Cloudflare | Pending brand/resource binding/migration/rollback contract |
| CF-4 pure/hybrid ownership | Cloudflare | Pending explicit state owners and missing upstream-route implementations |
| CI-1 same product contracts | Integration | Pending actual Compose/workerd/D1/DO runners, not runtime stubs |
| WL-2 macOS identity | White-label | Pending complete bundle/keychain/updater/package verification |
| WL-3 mobile identity | White-label | Pending iOS/Android IDs/extensions/callbacks/build verification |
| WL-4 Windows/Linux identity | White-label | Pending Electron installer/feed/update verification |
| WL-5 text/assets | White-label | Pending built-product branding sweep and runtime surfaces |
| WL-6 device/OTA identity | White-label | Pending BLE/DIS/NFC/model/OTA implementation and hardware evidence |
| WL-7 artifact matrix | White-label | Pending brand × target × platform build and release verification |
| SH-4 Linux operations/release | Server | Pending install/upgrade/restore/model acceptance and candidate manifest |
| CF-5 staging release | Cloudflare | Pending complete version-attributed candidate and live readiness/product evidence |

Engineering fixtures use synthetic brand names and temporary local data. Actual
distribution identities, signing/OTA keys and hardware results must be recorded
when available; they cannot be inferred from fixture builds. Access-control,
migration and release changes require explicit authorization before merging or
remote deployment under repository rules.
