# Eddy signed macOS candidate — native acceptance, 2026-09-08

Repository observation: `14091bedc6`, clean `codex/unified-delivery` worktree.
Artifact: `/private/tmp/eddy-macos-production-20260907-a/Eddy.app`, version
`0.1.0`, build `2026090701`, bundle `dev.summersmile1984.eddy.macos`.
This is the existing signed artifact; no code was rebuilt or re-signed.

## Verified on the exact artifact

- System-context `codesign --verify --deep --strict --verbose=2` passed, including
  nested frameworks and libraries. The actual signer is Developer ID Application
  for team `YTY9KEWQC5`, with hardened runtime and a secure signing timestamp.
  A sandbox-context invocation could not resolve the certificate and incorrectly
  reported an invalid signature; the normal system trust context resolved it.
- The app's built-in `--auth-storage-canary-result=<private path>` executed actual
  Keychain write, read-back and deletion. It returned
  `{"success":true,"stage":"complete"}` and exited with code 0. This uses a
  synthetic canary account before normal startup, not a real session.
- CUA opened the signed bundle itself. Its native window was `Eddy v0.1.0`.
  The rendered screenshot showed the geometric **e** logo, **Eddy**, and
  **懂你的随身AI伴侣**. Empty sign-in submission was disabled. Switching to
  account creation displayed the Name field and a disabled empty submit button;
  switching back restored sign-in. No account was created and no credentials
  were entered. The screenshot is in the task's native CUA output, not a separate
  archived PNG.
- The bundle's `Contents/Resources/ForkDeployment.json` exactly matches its
  staged profile, selects `cloudflare.production`, and disables environment URL
  overrides. Executable, ZIP, profile and build-manifest SHA256 values are
  recorded in the machine-readable proof.

## Remaining boundaries

At `2026-09-08T01:24:35.909529+00:00`, unauthenticated probes of the artifact's
configured production endpoints all returned **HTTP 404**:

- Edge `/health`
- Auth `/api/auth/get-session`
- Web `/`

This proves those public paths were unavailable at the observation time; it is
not an authenticated Cloudflare inventory of every Worker. Native login,
restoration, recording, ASR, LLM, TTS and the complete production business flow
remain unverified on this candidate.

`xcrun stapler validate` returned 65 and reported no stapled ticket. No notary
credentials were present in the process's documented App Store Connect/notary
variables or the exact `com.apple.gke.notary.tool` Keychain service query. This
limited lookup is not proof that every possible named profile is absent. No
notarization was submitted. Developer ID signing is not notarization.

The app is arm64 and declares its actual macOS 26.0 minimum; this observation ran
on macOS 26.5.2. Universal architecture and older-system acceptance are not
claimed. The original artifact manifest retains `notarized=false`,
`service_verified=false` and `release_ready=false`.

Evidence archive:
`/Users/macstudio/.codex/eddy-production/macos-signed-acceptance-20260908/`.
It contains the compact verification JSON, synthetic canary result, signature
and stapler outputs. No user credential, transcript or raw business data is
included. The current task's JIT schema/route patch remains separately pending
explicit authorization after its automatic approval rejection; this acceptance
does not apply that patch or mutate production resources.
