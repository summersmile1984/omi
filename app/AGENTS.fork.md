# App — Fork Rules (cloud-neutral / self-hosted)

Upstream rules live in [`AGENTS.md`](./AGENTS.md); this file adds only what is
true for this fork. It is fork-owned, so it never conflicts on an upstream sync.

## Native Better Auth staging

`fork/prepare.py` builds an isolated local Android app from a private brand and
an explicit `self_hosted.local` or `cloudflare.local` profile. Follow
[`fork/README.md`](fork/README.md). It replaces the old dev-issued JWT bridge in
the staged source with secure opaque sessions and short-lived derived JWTs;
never pass an issuer secret or Firebase custom token to a mobile artifact.
`fork/test.sh` is the registered local/CI contract runner. It compiles both Dart
entrypoints; native APK/UI verification remains a separately recorded local
gate. Flutter is pinned to 3.44.5/Dart 3.12.2 with the unchanged upstream lock.

## Brand-generated files

`scripts/brand/apply.py --brand <id> --only flutter` renders `lib/flavors.brand.dart`
from `brand/<id>/manifest.yaml`'s `brand.display_name` (source-of-truth
comment inline in the generated file). `lib/flavors.dart`'s `F.title` reads
`kBrandDisplayName` from it -- never edit either file to change the app
title, edit the manifest and re-run `apply.py`.

## Fork discipline

Do not modify upstream files under `app/`. Fork behavior belongs in fork-owned
files and package overrides; see
[`dev/unified-main/00-upstream-touch-policy.md`](../dev/unified-main/00-upstream-touch-policy.md).
