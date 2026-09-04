import { authCorsOptions } from "../../auth/shared/cors-policy.mjs";

export function betterAuthBridge(authHandler, baseUrl, trustedOrigins = []) {
  const cors = authCorsOptions([new URL(baseUrl).origin, ...trustedOrigins]);
  return async function handleBetterAuth(req, res) {
    const applyCors = () => {
      res.vary("Origin");
      const origin = cors.origin(req.headers.origin || "");
      if (origin) {
        res.setHeader("access-control-allow-origin", origin);
        res.setHeader("access-control-allow-credentials", "true");
        res.setHeader(
          "access-control-expose-headers",
          cors.exposeHeaders.join(", "),
        );
      }
    };
    if (req.method === "OPTIONS") {
      applyCors();
      if (cors.origin(req.headers.origin || "")) {
        res.setHeader(
          "access-control-allow-methods",
          cors.allowMethods.join(", "),
        );
        res.setHeader(
          "access-control-allow-headers",
          cors.allowHeaders.join(", "),
        );
      }
      return res.status(204).end();
    }
    try {
      const url = new URL(req.originalUrl, baseUrl).toString();
      const headers = new Headers();
      for (const [key, value] of Object.entries(req.headers)) {
        if (value !== undefined)
          headers.set(
            key,
            Array.isArray(value) ? value.join(", ") : String(value),
          );
      }
      let body = null;
      if (
        ["POST", "PUT", "PATCH"].includes(req.method) &&
        req.body !== undefined
      ) {
        body = JSON.stringify(req.body);
        headers.set("Content-Type", "application/json");
      }
      const request = new Request(url, { method: req.method, headers, body });
      const response = await authHandler(request);
      res.status(response.status);
      response.headers.forEach((value, key) => res.setHeader(key, value));
      applyCors();
      return res.send(await response.text());
    } catch (_error) {
      if (res.headersSent) {
        res.destroy();
        return undefined;
      }
      res.setHeader("cache-control", "no-store");
      res.setHeader("retry-after", "1");
      applyCors();
      return res.status(503).json({ error: "identity_store_unavailable" });
    }
  };
}
