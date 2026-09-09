# Upstream v0.12.292 local sync verification — 2026-09-05

## Scope

- Previous upstream base: `09ff17e4e5f649799c6e4f17d2af1e5ceb373eef`.
- Refreshed upstream base: `c4880cd5f627780632ed6077fa5940ce86e0f5d3`.
- Local merge commit: `4bf718a1cd1988fdf7a670d10f461ff7c6dc39d5`.

The two upstream commits only consolidate the macOS v0.12.292 changelog. The
merge changed `desktop/macos/CHANGELOG.json`, added its released changelog
record, and removed the matching unreleased record. The merge had zero
conflicts; no fork-owned source or profile input was edited during resolution.

## Checks

```text
git rev-list --left-right --count upstream/main...HEAD
=> 0 248

python3 scripts/fork/upstream_sync_plan.py --base HEAD --upstream upstream/main
=> conflicts: []

python3 scripts/fork/check-upstream-touch.py --base HEAD^1 --head HEAD
=> OK: 0 upstream file(s) changed, all within the allowlist.
```

The last command is intentionally merge-scoped. A full current-candidate check
against `upstream/main` reports 36 older source divergences, including backend
patch sites and desktop `AuthService.swift`. This merge does not add to that
debt, but the one-fork topology remains unaccepted until it is removed or
resolved through the existing zero-upstream-touch policy.

## Limits

This is only a local source sync. It has not been pushed, opened as a pull
request, merged to `main`, deployed, or release-qualified.
