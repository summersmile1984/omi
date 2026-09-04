// Both HTTP adapters expose the session credential required by bearer clients.
// The allowlist is deployment configuration; an unknown Origin is never echoed.
export function authCorsOptions(allowedOrigins) {
  return {
    origin: (origin) => (allowedOrigins.includes(origin) ? origin : ""),
    credentials: true,
    allowMethods: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allowHeaders: ["authorization", "content-type"],
    exposeHeaders: ["set-auth-token", "set-auth-jwt"],
  };
}
