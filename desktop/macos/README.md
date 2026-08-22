# OMI Desktop

macOS app for OMI — always-on AI companion. Swift/SwiftUI frontend, Python backend.

## Structure

```
Desktop/          Swift/SwiftUI macOS app (SPM package)
../../backend/    Python API server (Firestore, Redis, auth, LLM)
agent/            Agent runtime for multi-provider chat (TypeScript)
dmg-assets/       DMG installer resources
```

## Development

Requires macOS 14.0+, Python 3.11 with uv, and code signing with an Apple Developer ID.

```bash
# Run (builds Swift app, starts Python backend, launches app)
./run.sh

# Run an isolated named bundle for parallel testing
OMI_APP_NAME="omi-subagent-test" ./run.sh

# Run with the dev backend (skips local Python + tunnel)
./run.sh --yolo

# Keep one explicit focused regression test running after each save
./scripts/dev-feedback.py --watch swift 'ChatTests/testSendsMessage'
./scripts/dev-feedback.py --watch python 'tests/unit/test_desktop_chat.py'

# Relaunch an already-built named app without holding the terminal open.
# Supply a harness/external backend; --no-wait deliberately does not own one.
OMI_SKIP_BACKEND=1 OMI_APP_NAME="omi-subagent-test" ./run.sh --yolo --fast-only --no-wait

# Force a complete bundle refresh after changing packaged runtime inputs
./run.sh --full
```

`--yolo` targets the deployed development services. Those services currently use production Firebase identities and data stores, so use a named `omi-*` bundle for isolated desktop state and avoid treating it as an offline data sandbox.

`run.sh` auto-detects an `Apple Development` or `Developer ID Application` signing identity from your login keychain, then falls back to a self-signed `Omi Local Dev Signing` identity if you have one. Override with `OMI_SIGN_IDENTITY="..." ./run.sh`. See [`docs/local-code-signing.md`](docs/local-code-signing.md) for how to create that identity and why a signing identity's Team ID decides the local entitlements.

After a successful full launch, `run.sh` automatically uses its fast lane for ordinary Swift-only edits: it incrementally builds Swift, patches the already-installed app executable plus the current desktop API URL, re-signs it, and relaunches. Named local-harness profiles are eligible too; their current disposable `.env` is refreshed on every fast patch rather than cached. Changing package metadata, bundled resources, agent/runtime inputs, entitlements, or persistent launch configuration safely falls back to the complete packaging path. Use `./run.sh --full` (or `OMI_FORCE_FULL_BUNDLE=1`) to force that path; set `OMI_SCAN_STALE_BUNDLES=1` only when recovering from stale LaunchServices registrations.

`dev-feedback.py` is the fast test loop: pass an explicit XCTest filter or pytest path, use `--once` for one check or `--watch` to rerun after relevant saves. A filter matching zero tests is reported as a failure, not a pass. It never guesses coverage and never replaces `./test.sh`, which remains the full component/PR suite. That suite runs isolated Swift suites with four workers locally. CI uses two workers: each has a copy-on-write SwiftPM scratch directory plus an isolated Foundation runtime home, so filtered `--skip-build` processes do not share build locks, preferences, Application Support, caches, or temporary files. Use `OMI_SWIFT_TEST_SUITE_WORKERS=1` when diagnosing concurrency-sensitive behavior. `run.sh` reuses a healthy worktree-owned Python backend on Swift-only relaunches. Add `--no-wait` only when a harness or other external backend owns the API; it returns after the app launch instead of holding the terminal for launcher-managed processes.

Self-hosted onboarding does not send profile-derived web-research queries directly to DuckDuckGo. The optional enrichment is disabled at the client boundary; deployments that enable web search use the operator backend's explicit SearXNG transport instead of inheriting a managed/vendor endpoint.

`git push` is the bounded desktop acceptance gate: desktop source changes run only the fast `xcrun swift build -c debug --package-path Desktop` check on the installed Xcode. This is intentionally less than CI: the parallel, isolated Swift suite, clean release compile, and pinned `/Applications/Xcode_16.4.app` (Xcode 16.4 build 16F6) belong to GitHub Actions. Do not move those CI jobs into pre-push; preserving push-time budget keeps normal iteration fast. Use `dev-feedback.py --watch` while editing.

Named bundles derive an isolated bundle ID and OAuth callback URL scheme from `OMI_APP_NAME`. `Omi Dev` keeps `com.omi.desktop-dev` / `omi-computer-dev`, while `OMI_APP_NAME="omi-subagent-test"` uses `com.omi.omi-subagent-test` / `omi-omi-subagent-test`. The app reads that scheme from `CFBundleURLTypes` for OAuth redirects, so parallel dev bundles do not claim the canonical `omi-computer-dev` callback.

### Operator-owned self-hosted updates

A self-hosted macOS artifact may opt into Sparkle only when its signed bundle carries
`SUPublicEDKey` (a base64-encoded 32-byte Ed25519 public key) and the signed launch
configuration provides `OMI_UPDATE_FEED_URL`, for example
`https://updates.example.invalid/omi/macos/appcast.xml`. The feed must be an explicit
operator HTTPS endpoint with a path and no credentials, query, or fragment; Omi-owned
hosts are rejected. Sparkle still verifies every downloaded archive against the baked
public key, so an operator must publish an appcast and ZIPs signed by the matching
private key. `run.sh` receives the public half through
`OMI_UPDATE_SPARKLE_PUBLIC_KEY` and writes it into the signed bundle; it never
copies a managed key into a self-hosted artifact. Missing or invalid feed/key
metadata leaves updates typed-unavailable; there is no fallback to Omi's managed
feed. The operator must also smoke-test the signed bundle and retain the
key-rotation/recovery record outside this repository.

## License

MIT
