import { webProfile } from './web-profile';

export async function claimReferral(
  code: string,
  getToken: () => Promise<string | null>,
): Promise<{ claimed: boolean; trial_days: number }> {
  if (!code || code.length > 512) throw new Error('Invalid referral link.');
  const token = await getToken();
  if (!token) throw new Error('Sign in to claim this referral.');
  const response = await fetch(
    `${webProfile().api_base_url.replace(/\/$/, '')}/v1/users/me/referral/claim`,
    {
      method: 'POST',
      credentials: 'omit',
      cache: 'no-store',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
      signal: AbortSignal.timeout(20000),
    },
  );
  if (!response.ok)
    throw new Error('We could not apply this referral. Please try again.');
  const result = await response.json();
  if (typeof result?.claimed !== 'boolean' || result.trial_days !== 30)
    throw new Error('We could not verify the referral result. Please try again.');
  return result;
}

export function desktopDownloadUrl(): string {
  return `${webProfile().api_base_url.replace(/\/$/, '')}/v2/desktop/download/latest`;
}

export function navigateToDesktopDownload(): void {
  window.location.assign(desktopDownloadUrl());
}
