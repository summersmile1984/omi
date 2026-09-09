# Shared runtime telemetry

`fallback.mjs` owns the existing structured fallback event for Node and Workers.
`fallback.d.mts` defines the bounded event vocabulary. Runtime adapters import
this module directly; there is no second logger or legacy Workers alias.

The function serializes only the declared event fields. Callers must not include
PII, credentials, query text or provider error bodies. The optional request ID
is for an already sanitized correlation identifier.

Node authentication tests and the Cloudflare route suite exercise this producer
through real fallback branches. Both manifest lanes trigger on this directory.
The Auth Dockerfile copies it explicitly into `/app/runtime/shared`.
