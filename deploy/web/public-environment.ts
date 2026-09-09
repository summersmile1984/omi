import {
  parseWebProfile,
  parseWebPresentation,
  type WebPresentation,
} from "../../web/app/src/lib/fork/web-profile";

const PROFILE_KEYS = [
  "name",
  "target",
  "stage",
  "identity_provider",
  "api_base_url",
  "auth_base_url",
  "web_base_url",
  "mcp_base_url",
  "share_base_url",
  "objects_base_url",
  "auth_callback_scheme",
  "capabilities",
] as const;

export function publicEnvironment(
  input: WebPresentation & {
    product_name: string;
    profile: Record<string, unknown>;
  }
) {
  const profile = Object.fromEntries(
    PROFILE_KEYS.map((key) => [key, input.profile[key]])
  );
  const serialized = JSON.stringify(profile);
  const validated = parseWebProfile(serialized);
  const productName = input.product_name.trim();
  if (!productName) throw new Error("Web product name must be explicit");
  const presentation = JSON.stringify({
    tagline: input.tagline,
    support_email: input.support_email,
    links: Object.fromEntries(
      [
        "website",
        "download",
        "docs",
        "help",
        "feedback",
        "community",
        "privacy",
        "terms",
      ].map((key) => [
        key,
        input.links?.[key as keyof WebPresentation["links"]],
      ])
    ),
  });
  parseWebPresentation(presentation);
  const api = validated.api_base_url.replace(/\/+$/, "");
  const socket = new URL(api);
  socket.protocol = socket.protocol === "https:" ? "wss:" : "ws:";
  return {
    NODE_ENV: "production",
    NEXT_PUBLIC_API_BASE_URL: api,
    NEXT_PUBLIC_WS_BASE_URL: socket.href.replace(/\/+$/, ""),
    NEXT_PUBLIC_OMI_PROFILE_JSON: serialized,
    NEXT_PUBLIC_OMI_PRODUCT_NAME: productName,
    NEXT_PUBLIC_OMI_PRESENTATION_JSON: presentation,
  };
}

export function injectPublicEnvironment(
  client: string,
  values: ReturnType<typeof publicEnvironment>
): string {
  const newline = client.indexOf("\n");
  const banner = client.slice(0, newline);
  if (
    !banner.startsWith("globalThis.process = { env: ") ||
    !banner.endsWith(" };")
  ) {
    throw new Error(
      "Moonshine public-environment banner changed; review the build adapter"
    );
  }
  return `globalThis.process = { env: ${JSON.stringify(
    values
  )} };\n${client.slice(newline + 1)}`;
}
