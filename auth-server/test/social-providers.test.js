import assert from "node:assert/strict";
import test from "node:test";
import {
  assertRequiredSocialProviders,
  buildSocialProviders,
  SocialProviderConfigurationError,
} from "../src/social-providers.js";

test("builds only explicitly configured operator social providers", () => {
  assert.deepEqual(buildSocialProviders({}), {});
  assert.deepEqual(
    buildSocialProviders({
      AUTH_GOOGLE_CLIENT_ID: "operator-google-client",
      AUTH_GOOGLE_CLIENT_SECRET: "operator-google-secret",
    }),
    {
      google: {
        clientId: "operator-google-client",
        clientSecret: "operator-google-secret",
      },
    },
  );
});

test("partial or migration-incomplete social provider configuration fails closed", () => {
  assert.throws(
    () => buildSocialProviders({ AUTH_APPLE_CLIENT_ID: "operator-apple-client" }),
    SocialProviderConfigurationError,
  );
  assert.throws(
    () =>
      assertRequiredSocialProviders(new Set(["google", "apple"]), {
        AUTH_GOOGLE_CLIENT_ID: "operator-google-client",
        AUTH_GOOGLE_CLIENT_SECRET: "operator-google-secret",
      }),
    /requires configured social providers: apple/,
  );
});
