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
observed Developer ID team, and the account's observed workers.dev namespace.
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
```
