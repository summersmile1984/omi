export class SocialProviderConfigurationError extends Error {
  constructor(message) {
    super(message);
    this.name = "SocialProviderConfigurationError";
  }
}

const PROVIDERS = Object.freeze({
  google: {
    clientId: "AUTH_GOOGLE_CLIENT_ID",
    clientSecret: "AUTH_GOOGLE_CLIENT_SECRET",
  },
  apple: {
    clientId: "AUTH_APPLE_CLIENT_ID",
    clientSecret: "AUTH_APPLE_CLIENT_SECRET",
  },
});

export function buildSocialProviders(env = process.env) {
  const result = {};
  for (const [provider, settings] of Object.entries(PROVIDERS)) {
    const clientId = env[settings.clientId]?.trim();
    const clientSecret = env[settings.clientSecret]?.trim();
    if (Boolean(clientId) !== Boolean(clientSecret)) {
      throw new SocialProviderConfigurationError(
        `${settings.clientId} and ${settings.clientSecret} must be set together`,
      );
    }
    if (clientId && clientSecret) {
      result[provider] = { clientId, clientSecret };
    }
  }
  return Object.freeze(result);
}

export function assertRequiredSocialProviders(required, env = process.env) {
  const configured = new Set(Object.keys(buildSocialProviders(env)));
  const missing = [...required].filter((provider) => !configured.has(provider));
  if (missing.length) {
    throw new SocialProviderConfigurationError(
      `identity import requires configured social providers: ${missing.sort().join(", ")}`,
    );
  }
}
