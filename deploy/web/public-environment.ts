import { parseWebProfile } from '../../web/app/src/lib/fork/web-profile';

const PROFILE_KEYS = [
  'name',
  'target',
  'stage',
  'identity_provider',
  'api_base_url',
  'auth_base_url',
  'web_base_url',
  'mcp_base_url',
  'share_base_url',
  'objects_base_url',
  'auth_callback_scheme',
  'capabilities',
] as const;

export function publicEnvironment(input: {
  product_name: string;
  profile: Record<string, unknown>;
}) {
  const profile = Object.fromEntries(
    PROFILE_KEYS.map((key) => [key, input.profile[key]]),
  );
  const serialized = JSON.stringify(profile);
  const validated = parseWebProfile(serialized);
  const productName = input.product_name.trim();
  if (!productName) throw new Error('Web product name must be explicit');
  const api = validated.api_base_url.replace(/\/+$/, '');
  const socket = new URL(api);
  socket.protocol = socket.protocol === 'https:' ? 'wss:' : 'ws:';
  return {
    NODE_ENV: 'production',
    NEXT_PUBLIC_API_BASE_URL: api,
    NEXT_PUBLIC_WS_BASE_URL: socket.href.replace(/\/+$/, ''),
    NEXT_PUBLIC_OMI_PROFILE_JSON: serialized,
    NEXT_PUBLIC_OMI_PRODUCT_NAME: productName,
  };
}

export function injectPublicEnvironment(
  client: string,
  values: ReturnType<typeof publicEnvironment>,
): string {
  const newline = client.indexOf('\n');
  const banner = client.slice(0, newline);
  if (!banner.startsWith('globalThis.process = { env: ') || !banner.endsWith(' };')) {
    throw new Error(
      'Moonshine public-environment banner changed; review the build adapter',
    );
  }
  return `globalThis.process = { env: ${JSON.stringify(values)} };\n${client.slice(
    newline + 1,
  )}`;
}
