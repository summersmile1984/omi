# WL-6 firmware identity verification — 2026-09-05

This local candidate uses `brand/<id>/manifest.yaml` as the one identity input
for CV1 staging and both deployment targets. It does not sign, publish, flash
or contact a real device.

## Covered behavior

- `scripts/brand/generators/firmware.py` renders a fork-owned
  `backend/fork/firmware_brand.generated.json`. Its public release portion has
  a brand ID, accepted DIS model/neutral hardware alias, release-tag prefix,
  OTA asset prefix and repository release URL. The staging-only portion holds
  BLE/DIS/NFC identity and a signing-key reference, never a key value.
- `omi/firmware/fork/stage.py` copies `omi/firmware/omi` into a new output,
  changes only the staged `omi.conf` and NFC pairing literal, records hashes,
  and refuses an existing output. It leaves upstream firmware files unchanged.
- The Server OS derived-image Dockerfile renders the same generated input. Its
  fork seams replace the upstream model/prefix/release source only for
  `self_hosted`; the release adapter reads `FIRMWARE_RELEASE_MANIFEST_URL` and
  returns no GitHub fallback.
- Cloudflare resource plans put a validated public policy in
  `FIRMWARE_BRAND_POLICY_JSON`. API Core returns 503 for a missing or malformed
  policy and filters model, release tag and ZIP asset by the selected brand.

## Local evidence

```text
python3 scripts/brand/test_brand_tooling.py
39 tests passed

cd backend && .venv/bin/python -m pytest fork/tests/test_firmware.py -q
3 passed

cd backend && ENCRYPTION_SECRET=<32-byte test value> .venv/bin/python -m pytest fork/tests/test_real_seams.py -q
4 passed

backend `fork-selfhost-startup` selected contract (21 files, including firmware)
passed through `bash test.sh`

cd deploy/cloudflare/python/api-core && uvx uv==0.12.3 run pytest -q tests/test_entry.py
29 passed

cd deploy/cloudflare && ./node_modules/.bin/vitest run tests/resource-plan.test.mjs tests/local-target.test.mjs
26 passed

bash deploy/self-host/ci/product.sh
fresh derived Server OS image, migrations and identity/onboarding/Tasks core 8 passed

bash deploy/cloudflare/ci/product.sh
local seven-Worker target: core 8, recording 6, chat 11 and share 2 passed
```

The normal `npm test` wrapper for the narrowed Cloudflare Vitest selection was
blocked before tests by the independent manifest check reporting the newly
unclassified upstream Redis symbol `release_daily_summary_lock`; direct Vitest
executed the relevant resource-plan and local-target tests. The separate local
Cloudflare product gate completed all four route suites above.

## Remaining release gates

The policy does not prove a private signing key, an NCS/MCUboot binary, a
vendor/Cloudflare/Server remote release, BLE/DIS/NFC hardware values, or an OTA
flash/rollback. Those require a real brand key, a reviewed target account and
physical device evidence; they are intentionally not inferred from fixture
output.
