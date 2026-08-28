import type { UserSubscription } from '@/types/user';

export function shouldShowSubscriptionUpgrade(
  subscription: Pick<UserSubscription, 'show_subscription_ui'> | null | undefined,
): boolean {
  return subscription?.show_subscription_ui !== false;
}
