# `brand/` — one manifest per identity, `main` stays neutral

Design record: [`dev/unified-main/04-brand-layer.md`](../dev/unified-main/04-brand-layer.md). This file is the short version.

## What lives here

```
brand/
├── _schema/manifest.schema.json   # the enforced shape -- apply.py and check.py both validate against it
├── _allow.yaml                     # named exemptions from check.py's leak scan, each with a reason
├── omi-upstream/manifest.yaml      # upstream's OWN identity, recorded for regression -- not a template
└── <brand>/manifest.yaml           # one directory per real fork brand (none exist yet as of B0)
```

## The two tools

```bash
scripts/brand/apply.py --brand <id> [--only flutter|desktop|windows|backend|firmware|web|docs|ci]
scripts/brand/check.py --brand <id> [--baseline PATH]
```

`apply.py` renders every generated file a brand needs from its manifest — idempotent, so running it twice produces zero diff. `check.py` scans for a *different* brand's identity leaking into this brand's build (upstream's "Omi"/`omi.me`/`com.omi.*` showing up somewhere it shouldn't), and separately checks that `apply.py --check-clean` still holds.

As of B0, `apply.py` has a manifest-loading and validation path but zero registered generators — every `--only` category is a real name today, but none has a renderer yet. Each of B1 through B7 registers exactly one. Read `apply.py`'s own module docstring before assuming `--brand omi-upstream` producing a clean diff means anything beyond "the manifest parses."

## Adding a real brand

1. Copy `omi-upstream/manifest.yaml` to `<your-brand-id>/manifest.yaml` and fill in every field — `scripts/brand/schema_validate.py` (via `apply.py`) will refuse anything that doesn't match `_schema/manifest.schema.json`.
2. Add `assets/`, `fonts/`, `legal/`, `prompts/persona.yaml` per §1 of the design doc. None of these are read yet — the same "no generator registered" situation as everything else in B0.
3. Once B1+ land, `apply.py --brand <your-brand-id>` and `check.py --brand <your-brand-id>` are the acceptance gate for every subsequent PR in the B-series.

## Private brand assets

A brand's `assets/`, `fonts/`, `legal/`, and prompt/persona files can live in a private repo instead of this public fork — see §7 of the design doc. `manifest.yaml` itself, `_schema/`, and `omi-upstream/` always stay in this repo; only the brand-specific content directories are meant to be swappable.
