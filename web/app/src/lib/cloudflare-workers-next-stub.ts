// Used only by the ordinary Node-based Next.js build. vinext/workerd resolves
// the real `cloudflare:workers` module and supplies the configured bindings.
export const env: Record<string, unknown> = {};

// Vinext's Cloudflare adapter inspects these runtime exports while building
// the ordinary Node/Next-compatible bundle. They are never instantiated by
// the Node fallback; the real Worker runtime provides the platform classes.
export class WorkerEntrypoint {}
export class DurableObject {}
export class WorkflowEntrypoint {}
