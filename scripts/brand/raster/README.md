# Shared brand raster boundary

This fork-owned package validates and resizes the raster inputs from the single
brand manifest. Electron, macOS and Flutter platform generators import
`png.mjs`; they do not read another brand file or carry another image decoder.

Inputs must be relative, in-tree, bounded, static 8-bit PNG files. The master
icon is square and at least 1024 pixels. Missing, escaping, animated, corrupt,
fully transparent or oversized inputs fail generation before a platform writes
replacement resources. `fixture.mjs` creates synthetic H/N inputs only in a
caller-provided temporary directory.

Run with Node 22:

```bash
npm ci --prefix scripts/brand/raster --ignore-scripts --no-audit --no-fund
npm test --prefix scripts/brand/raster
```

This package proves raster input and resize behavior. Each platform still owns
its output layout, runtime decoder and release qualification.
