# Windows release history verification

The Windows workflow publishes a GitHub prerelease tagged
`v<version>-windows`. The release contains the versioned NSIS installer, its
`.exe.blockmap`, and `latest.yml`. Before a future Cloudflare projection is
designed, `.github/scripts/windows_release_history.py` provides a bounded,
read-only handoff for one operator-exported release.

The export contract is intentionally separate from the existing macOS
`desktop-release-manifest-v1` contract. It requires:

- `release.release_id` in the workflow's `v<semver>-windows` form, matching
  `release.version`, plus an explicit positive `build_number`;
- `release.prerelease` and `release.channel` to agree (`true`/`beta` or
  `false`/`stable`);
- exact `https://github.com/BasedHardware/omi/releases/download/...` URLs for
  `Omi-for-Windows-Setup-<version>.exe`, its `.blockmap`, and `latest.yml`, each
  with a lowercase 64-character SHA-256 digest;
- a `source.release_fingerprint` that binds the export to the original
  GitHub-release snapshot.

The verifier rejects unknown fields, credentials, control characters, encoded
path traversal, query/fragment-bearing URLs, alternate GitHub repositories,
and exports above 256 KiB. It emits a timestamp-free content-bound plan whose
`plan_hash` is stable across JSON key ordering:

```bash
python3 .github/scripts/windows_release_history.py \
  --export /path/to/windows-release-export.json \
  --output /tmp/windows-release-plan.json
```

The default operation is always dry-run. The script does not call GitHub, copy
the `.exe`, `.blockmap`, or `latest.yml`, write D1/R2, create a channel pointer,
or promote Beta to Stable. A reviewed staging executor now accepts this plan
through `/internal/windows-release-history/reviews`, records a D1 review ledger,
and after explicit apply mirrors exactly the three assets into the isolated
`DESKTOP_UPDATES` R2 namespace. Jobs follows only canonical GitHub URLs and
trusted signed CDN redirects, streams a bounded SHA-256 check, verifies R2
metadata/size, and retries or terminally rejects digest conflicts. It remains
an artifact transfer ledger only: it does not make a Windows row publicly
readable, create a channel pointer, or mutate desktop release authority.

The release workflow's real provider credentials and GitHub release history are
not present in CI fixtures. Staging tests cover fail-closed review, idempotent
queueing/copy, signed-redirect handling, and digest mismatch cleanup, but this
slice does not claim historical artifact replay, channel promotion, public
download cutover, or production parity.
