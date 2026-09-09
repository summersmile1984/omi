# Beta CD preparation evidence — 2026-09-09

The independent Cloudflare and Server CD workflows consume a successful full
Fork Checks SHA and frozen archives. GitHub environments and credentials are
configured; the Cloudflare beta storage resources and Server Tunnel exist.
This record does not claim a completed public deployment.

Server source `2be37887f9` was built and booted as the isolated Docker Desktop
project `eddy-preview-2be37887`, using the beta profile and production Auth
guards. MiMo owns chat/ASR/TTS; local Ollama owns BGE-M3 Embedding. No default
prompt was changed. The twelve containers, including the completed artifact
check, reached the expected states.

Observed commands and results:

- `contracts/deployment/core.py --metadata <private preview metadata> --remote`:
  all 16 business cases and two owned-account cleanups passed after separating
  the loopback transport origin from the public referral issuer.
- `scripts/fork/server_ai_acceptance.py --metadata <private preview metadata>`:
  authenticated streaming chat and persisted history passed; real TTS WAV to
  desktop PCM ASR passed and recognized the synthetic work-notes phrase.
  The upload-ASR case remains failed in this isolated preview because the
  public object hostname has no serving gateway yet. The request reached that
  hostname and returned 502 before ASR. Public CD still requires this case.
- The accepted backend's actual Embedding driver returned 1024 dimensions
  from the locally provisioned and hash-verified BGE-M3 artifacts.
- Focused release preparation, installed-image, hosted-core and Compose-model
  tests cover native and MiMo image roles, rejected missing/extra images,
  unchanged source identity and public-origin retention. The shared fork CI
  script lane executes these tests.

Corrections from live acceptance use the existing wire contracts: chat's
`utils.chat_followup.split_followup_tail` trims whitespace before a follow-up
chip; the multipart ASR endpoint requires `files` and accepts WAV, WebM, MP4
or sync BIN input. The fixture uses WAV. It continues to reject changed or
missing streamed answer content. No inference output is rewritten.

Preparation now derives image roles from the same selected profile as Compose:
MiMo requires backend/Auth/Web images; native profiles additionally require
the local LLM runtime. Building the old unconditional LLM image would read a
missing LLM configuration from the MiMo backend image.

The main-relative upstream preflight exceptions remain tracked under
`CI-FORK-2026-09-09-1` in `fork-first-push-2026-09-09.md`. The bounded push gate
uses the previously published feature head. Fork actionlint and additive
settings classification run normally; the incompatible upstream equivalents
use their existing explicit hatches. Full CI and public CD are not bypassed.
