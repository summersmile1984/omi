# Cloudflare public-share binding verification

Candidate code: `c29b84cfe5` on `codex/unified-delivery`.

The staged Cloudflare Web Worker uses the release resource plan's `EDGE`
service binding for anonymous share previews. It must not fall back to a
public cross-origin API request, because that would bypass the selected Worker
topology and made the local path return 502.

The local proof built the Cloudflare Web artifact with the existing pinned Bun
toolchain, started a disposable seven-Worker target with D1 and Worker service
bindings, then started the staged Web Worker with its `EDGE` binding pointed at
that target. Wrangler reported the binding as connected. A synthetic Better
Auth owner created one Task share and one Chat share; anonymous requests to the
Web public relay returned 200 for both. Each response contained only the
display payload and included `Cache-Control: private, no-store`.

The failure was specific to workerd service bindings: Request init options
`redirect: 'error'` and an `AbortSignal` cannot be transferred through that
binding. `public-proxy.ts` now applies both only to its external-fetch path.
The binding path is confined to the owned Edge service and uses the default
redirect mode. `share-experience.test.tsx` asserts that the bound request does
not receive the external timeout or redirect mode.

Verification commands and results:

```text
PATH=... bash web/app/fork/test.sh
# Bun auth: 14 passed; Vitest: 3 files, 19 passed

bun deploy/web/build.ts --target cloudflare --stage local --brand omi-upstream ...
# Built 28 routes for cloudflare.local

# Local staged Web -> EDGE -> API proof
web-edge-task-share-preview: 200, private/no-store, private fields absent
web-edge-chat-share-preview: 200, private/no-store, private fields absent
```

The disposable data and sanitized result are under
`/tmp/memweft-implementation/cloudflare/share-public/web-binding-results.json`.
This is local acceptance evidence only. It does not create Cloudflare
resources, deploy a Worker, qualify a custom domain, or qualify a release.
