# Eddy

**懂你的随身AI伴侣**

The SVG masters are original geometric artwork: an open current forms a
lowercase **e**. Warm white `#FAFAF7` and charcoal `#202422` keep the same mark
legible in the Dock, menu bar and sign-in screen.

- `assets/icon-master.svg` is the application icon, including its safe inset.
- `assets/logo-dark.svg` is the dark mark for light backgrounds.
- `assets/logo-light.svg` is the light mark for dark backgrounds.
- The matching PNGs are rasterizations consumed by the existing shared brand
  pipeline; the editable source remains SVG. They were rendered with
  `@resvg/resvg-js@2.6.2`, at width 1024 for the app icon and 512 for each mark.

The manifest records the requested product name and tagline, the locally
observed Developer ID team, and the operator's `smartipproxy.com` zone.
Public endpoint configuration is not a deployment receipt. The first desktop
version is `0.1.0`; upstream version history is preserved in Git rather than
presented as Eddy's own release history.

Native telemetry and Sparkle are disabled. Firmware signing, mobile store
registration, public privacy/terms pages and public update distribution have
not been provisioned by this identity package. The manifest reserves their
brand identity without claiming those services are live. API transport prefixes
remain compatible with the current shared client/server contract.

Validate the production profile with:

```sh
python3 scripts/profiles/render.py --brand eddy --target cloudflare --stage production --emit-json
python3 scripts/profiles/render.py --brand eddy --target self_hosted --stage production --emit-json
```

`deployments.<target>.<stage>` owns the independent public origins. Production
API hosts are `eddy-cf-api.smartipproxy.com` (Workers) and
`eddy-server-api.smartipproxy.com` (Server through Tunnel). Web/share hosts are
`eddy-cf.smartipproxy.com` and `eddy-server.smartipproxy.com`; auth has matching
`eddy-cf-auth` / `eddy-server-auth` hosts. MCP uses each target's own API host.
CF object access keeps the authenticated API route; Server object operations
use `eddy-server-objects.smartipproxy.com`. This does not make private R2
objects public. Beta inserts `-beta` after `eddy-cf` / `eddy-server`; local
profiles still use loopback addresses.

On 2026-09-09 the Cloudflare dashboard showed this zone active, with no DNS
record matching `eddy`. The configured names are delivery inputs; DNS/Worker
custom-domain and Tunnel bindings still need the corresponding deployed
services. See [the delivery guide](../../scripts/fork/RELEASE.md) for routing.
