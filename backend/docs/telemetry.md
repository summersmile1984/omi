# Backend telemetry deployment boundary

Backend PostHog events are optional product/integration analytics. The
`product_telemetry`, `integration_telemetry`, and conversation-memory
telemetry helpers construct the PostHog client lazily and only for managed
deployment profiles.

`OMI_DEPLOYMENT_PROFILE=neutral`, `self_hosted`, or `self-hosted` is an
egress boundary: those profiles skip PostHog client import and construction
before reading `POSTHOG_PROJECT_API_KEY`, `POSTHOG_API_KEY`, or
`POSTHOG_HOST`. This remains true if managed secrets are accidentally present
in the process environment. The MCP analytics surface uses the shared
integration helper and inherits the same guard.

Self-hosted deployments retain structured logs and local metrics as configured
by the operator; they do not silently contact the Omi PostHog host. Managed
deployments continue to use the explicitly configured `POSTHOG_HOST` and
project key. Telemetry is fail-open and must never change the owning request,
sync, or memory-extraction result.
