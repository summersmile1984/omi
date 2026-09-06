import { getIdToken } from '@/lib/firebase';
import { claimReferral } from '@/lib/fork/referrals';
export { navigateToDesktopDownload } from '@/lib/fork/referrals';

export type ReferralEnvironment = 'dev' | 'prod';
export interface ReferralClaimResult {
  claimed: boolean;
  trial_days: number;
}
export function parseReferralEnvironment(
  value: string | null,
): ReferralEnvironment | null {
  return value === 'dev' || value === 'prod' ? value : null;
}
export async function claimReferralTrial(
  code: string,
  _environment: ReferralEnvironment,
): Promise<ReferralClaimResult> {
  // The selected deployment owns the destination; URL query data cannot select a backend.
  return claimReferral(code, getIdToken);
}
