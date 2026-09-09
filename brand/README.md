# Brand manifests and generated build overlays

One `brand/<id>/manifest.yaml` identifies a product; deployment target and stage
remain independent. The manifest loader is shared by brand generation, scanning
and profile rendering. `--manifest PATH` loads a validated private YAML/JSON
overlay without adding a real brand to the public repository. When `--brand` is
also supplied, it must match `brand.id`.

Optional `self_hosted_inference.<stage>` selects `native` or `mimo-cn` for a
Server stage. The same renderer freezes that public selection into backend,
Web and native client profiles. No credentials belong in the manifest. An
explicit `--operator-ai` argument cannot contradict a declared brand selection.

## Current coverage

The Flutter runtime title generator and the CV1 firmware identity generator
exist. The firmware generator writes the fork-owned
`backend/fork/firmware_brand.generated.json`: it holds BLE/DIS/NFC build input,
the signing-key **reference**, and a smaller public OTA lookup policy. Native
package identities, assets, desktop, broader backend/Web/docs/CI generation and
artifact packaging remain work from [the white-label action
plan](../dev/unified-main/audit-2026-09-04/03-whitelabel-action-plan.md).
`omi-upstream` is a regression identity, not a deployable template for a new brand.

```bash
python3 scripts/brand/apply.py --brand omi-upstream --check-clean --json
python3 scripts/brand/apply.py --manifest /private/manifest.yaml --output-root /tmp/brand-build
python3 scripts/brand/check.py --manifest /private/manifest.yaml --output-root /tmp/brand-build --json
```

## CV1 firmware and OTA policy

Generate a private build input and stage an isolated CV1 source tree as follows:

```bash
python3 scripts/brand/apply.py --manifest /private/manifest.yaml --only firmware --output-root /tmp/brand-build
python3 omi/firmware/fork/stage.py --config /tmp/brand-build/backend/fork/firmware_brand.generated.json --output /tmp/brand-firmware
```

The stage contains a copied `omi.conf` and NFC source with the manifest's BLE,
DIS and pairing URL values. It also writes `firmware-release-policy.json` and
an attestation that identifies the signing-key reference without reading the
key. The command rejects pre-existing output, unsafe Kconfig values, malformed
release prefixes, a pairing URL without exactly one device-ID placeholder, and
URLs that exceed the CV1's 64-byte NFC URI buffer.

The Server OS Dockerfile renders the same policy into the derived image. Its
fork patch accepts only that image's device model/tag/asset prefix and fetches
the configured operator manifest; it has no GitHub fallback. The Cloudflare
resource plan projects only the public policy to `api-core` as
`FIRMWARE_BRAND_POLICY_JSON`; the Worker returns 503 if it is absent or invalid
and rejects device models, tags and assets from another brand. The signing key,
NCS/MCUboot build, remote publish and hardware OTA remain release operations,
so the firmware generator is partial and never makes `--release` ready.

Generators return relative paths and UTF-8 text in memory. The writer owns disk
changes. `--check-clean` compares exact bytes without writing, including missing,
untracked and already-dirty files; it does not use Git status as proof.
The JSON report names `supported`, `requested`, `rendered`, `skipped`, `partial`,
`files`, `drift` and `release_ready`. A successful development check may still
report partial coverage. `--release` fails before writing if any requested
category is missing or incomplete; the title-only Flutter generator is partial.

## Explicit deployment endpoints

Common `domains` may define `auth_base`, `mcp_base` and `objects_base` alongside
`api_base`, `web_app` and `share_base`. There is no implicit auth/MCP fallback to
API. For a brand deployed to both targets, use explicit overrides:

```yaml
deployments:
  self_hosted:
    production:
      api_base: https://api.server.example
      auth_base: https://identity.example
      web_app: https://app.server.example
      mcp_base: https://mcp.server.example
      share_base: https://share.server.example
      objects_base: https://objects.server.example
  cloudflare:
    production:
      api_base: https://api.edge.example
      auth_base: https://identity.example
      web_app: https://app.edge.example
      mcp_base: https://mcp.edge.example
      share_base: https://share.edge.example
      objects_base: https://objects.edge.example
```

The schema permits `production`, `beta` and `local`; override keys use the same
six names as `domains`. Overrides win over common values, including local target
defaults. Each resolved nonlocal endpoint must be an explicit HTTPS URL; using
API as the MCP/object proxy is allowed only by explicitly providing that URL.
Shared auth origins across targets are supported; these fields do not choose or
change JWT audience. Unknown fields and missing fork endpoints fail.

```bash
python3 scripts/profiles/render.py --target self_hosted --stage production --manifest /private/manifest.yaml --emit-json
python3 scripts/profiles/render.py --target cloudflare --stage local --manifest /private/manifest.yaml --emit-json
```

`--stage` selects one row so local use does not need fictional production/beta
origins. Omit it to generate all three stages. The resolved JSON schema and five
output paths remain unchanged. `--output-root` supports an isolated build tree;
`--check` requires all five expected files and never writes.

## Scanning and release evidence

The default `check.py` scan is a **source heuristic**: ARB values, Swift/Dart UI
literals, JS/TS string/template literals and JSX text/attributes (including
`web/app`), plist and listed prompt/config sources. It cannot execute dynamic
expressions or prove what a compiled app displays. `omi-upstream` reports match
counts and is explicitly a self-check, not a zero-leak result for other brands.

Use `--build-root DIR` for decoded artifact metadata/text and `--runtime-root DIR`
for exported prompts, HTML, OpenAPI or notification payloads. Each must contain
supported UTF-8 text. Reports list inspected and unscanned files; binaries,
images, signatures and live UI are not certified. These exports are additive to
a development source scan. `--release` scans the exports instead of source,
requires both roots plus complete generators, and forbids a ratchet baseline.
It is a gate within the later build/installation matrix, not a substitute for
signed builds, UI, OAuth or OTA acceptance.

```bash
python3 scripts/brand/check.py --manifest /private/manifest.yaml --output-root /tmp/brand-build --release --build-root /tmp/decoded-build --runtime-root /tmp/runtime-exports --json
```

`--baseline PATH` is only a source-inventory ratchet. It cannot bypass missing or
invalid manifests, stale generated bytes, or release completeness. Legal/history
exemptions are explicit in `_allow.yaml`; export exemption paths use `build/` or
`runtime/` prefixes. Preserve original licenses and authors.

## Local and CI checks

```bash
python3 -m unittest discover -s scripts/brand -p 'test_*.py'
python3 scripts/profiles/test_profiles.py
python3 scripts/profiles/check_tables.py
scripts/fork/preflight
```

The default profile checker verifies the upstream target and all five checked-in
outputs. The hermetic profile suite generates temporary brands for both fork
targets and all stages, including negative endpoint cases; it is registered in
`checks-manifest.fork.yaml` for both local and CI lanes. For a real generated
fork build, pass `--target`, `--manifest` and `--output-root` to `check_tables.py`.
No fixture brand, credential or generated build artifact is committed.
