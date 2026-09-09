export interface WebProfile {
  name: string;
  target: 'self_hosted' | 'cloudflare';
  stage: 'local' | 'beta' | 'production';
  identity_provider: 'better_auth';
  api_base_url: string;
  auth_base_url: string;
  web_base_url: string;
  mcp_base_url: string;
  share_base_url: string;
  objects_base_url: string;
  auth_callback_scheme: string;
  capabilities: { push_provider: string; [key: string]: unknown };
}

/** Only the explicitly selected, build-validated profile enters a fork client. */
export function parseWebProfile(serialized: string | undefined): WebProfile {
  if (!serialized) throw new Error('The Web deployment profile is missing.');
  const value = JSON.parse(serialized) as WebProfile;
  if (
    !value ||
    !['self_hosted', 'cloudflare'].includes(value.target) ||
    !['local', 'beta', 'production'].includes(value.stage) ||
    value.name !== `${value.target}.${value.stage}` ||
    value.identity_provider !== 'better_auth' ||
    !value.auth_callback_scheme ||
    !value.capabilities ||
    value.capabilities.push_provider !==
      (value.target === 'self_hosted' ? 'disabled' : 'webhook')
  )
    throw new Error('The Web deployment profile is invalid.');
  for (const key of [
    'api_base_url',
    'auth_base_url',
    'web_base_url',
    'mcp_base_url',
    'share_base_url',
    'objects_base_url',
  ] as const) {
    if (typeof value[key] !== 'string' || !value[key])
      throw new Error(`The profile requires ${key}.`);
    const url = new URL(value[key]);
    if (
      !['http:', 'https:'].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.search ||
      url.hash ||
      (value.stage !== 'local' && url.protocol !== 'https:')
    )
      throw new Error(`The profile has an invalid ${key}.`);
    if ((key === 'auth_base_url' || key === 'web_base_url') && url.pathname !== '/') {
      throw new Error(`${key} must be an origin.`);
    }
  }
  return value;
}

export function webProfile(): WebProfile {
  return parseWebProfile(process.env.NEXT_PUBLIC_OMI_PROFILE_JSON);
}

export function mcpServerUrl(): string {
  return `${webProfile().mcp_base_url.replace(/\/$/, '')}/v1/mcp/sse`;
}

export function directModelProvidersEnabled(): boolean {
  const enabled = webProfile().capabilities.allow_direct_model_providers;
  if (typeof enabled !== 'boolean')
    throw new Error('The direct model provider capability is missing.');
  return enabled;
}

export function productName(): string {
  const name = process.env.NEXT_PUBLIC_OMI_PRODUCT_NAME?.trim();
  if (!name) throw new Error('The Web product name is missing.');
  return name;
}

export interface WebPresentation {
  tagline: string;
  support_email: string;
  links: Record<
    | 'website'
    | 'download'
    | 'docs'
    | 'help'
    | 'feedback'
    | 'community'
    | 'privacy'
    | 'terms',
    string
  >;
}

export function parseWebPresentation(serialized: string | undefined): WebPresentation {
  if (!serialized) throw new Error('The Web presentation is missing.');
  const value = JSON.parse(serialized) as WebPresentation;
  if (
    typeof value?.tagline !== 'string' ||
    typeof value.support_email !== 'string' ||
    !/^[^\s@]+@[^\s@]+$/.test(value.support_email) ||
    !value.links
  )
    throw new Error('The Web presentation is invalid.');
  for (const key of [
    'website',
    'download',
    'docs',
    'help',
    'feedback',
    'community',
    'privacy',
    'terms',
  ] as const) {
    const link = value.links[key];
    if (key === 'community' && link === '') continue;
    if (typeof link !== 'string' || !link || /[\x00-\x20\x7f]/.test(link))
      throw new Error(`The Web presentation requires ${key}.`);
    const url = new URL(link);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password)
      throw new Error(`The Web presentation has an invalid ${key}.`);
  }
  return value;
}

export function webPresentation(): WebPresentation {
  return parseWebPresentation(process.env.NEXT_PUBLIC_OMI_PRESENTATION_JSON);
}
