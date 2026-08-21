export function betterAuthBridge(authHandler, baseUrl) {
  return async function handleBetterAuth(req, res) {
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
      return res.send(await response.text());
    } catch (_error) {
      if (res.headersSent) {
        res.destroy();
        return undefined;
      }
      res.setHeader("cache-control", "no-store");
      res.setHeader("retry-after", "1");
      return res.status(503).json({ error: "identity_store_unavailable" });
    }
  };
}
