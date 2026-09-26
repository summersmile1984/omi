/**
 * Brand identity for the web client. Values are injected at build time from
 * the deployment profile's brand manifest via NEXT_PUBLIC_BRAND_* env vars; the
 * default 'Omi' identity keeps local development consistent with the upstream
 * brand when no manifest override is provided.
 *
 * Pages that print the product name should import `BRAND` rather than typing
 * the literal string, so a profile swap is a one-line env change instead of a
 * search-and-replace across components.
 */

export interface WebBrand {
  displayName: string;
  shortName: string;
  tagline: string;
  appTitle: string;
}

const DEFAULT_BRAND: WebBrand = {
  displayName: 'Omi',
  shortName: 'Omi',
  tagline: 'thought to action',
  appTitle: 'Omi - Your AI Companion',
};

function readString(envValue: string | undefined, fallback: string): string {
  const trimmed = (envValue ?? '').trim();
  return trimmed.length > 0 ? trimmed : fallback;
}

export const BRAND: WebBrand = {
  displayName: readString(
    process.env.NEXT_PUBLIC_BRAND_DISPLAY_NAME,
    DEFAULT_BRAND.displayName,
  ),
  shortName: readString(
    process.env.NEXT_PUBLIC_BRAND_SHORT_NAME,
    DEFAULT_BRAND.shortName,
  ),
  tagline: readString(process.env.NEXT_PUBLIC_BRAND_TAGLINE, DEFAULT_BRAND.tagline),
  appTitle: readString(process.env.NEXT_PUBLIC_BRAND_APP_TITLE, DEFAULT_BRAND.appTitle),
};

export function loginPageTitle(): string {
  return `Sign In to ${BRAND.displayName}`;
}

export function loginImageAlt(): string {
  return `${BRAND.displayName} Product`;
}

export function ariaBrandLabel(): string {
  return BRAND.displayName;
}
