import { describe, expect, it } from 'vitest';

import { shouldShowSubscriptionUpgrade } from './subscription-ui';

describe('shouldShowSubscriptionUpgrade', () => {
  it('hides checkout when the account response explicitly disables it', () => {
    expect(shouldShowSubscriptionUpgrade({ show_subscription_ui: false })).toBe(false);
  });

  it('preserves the legacy visible default for older responses', () => {
    expect(shouldShowSubscriptionUpgrade({ show_subscription_ui: true })).toBe(true);
    expect(shouldShowSubscriptionUpgrade({})).toBe(true);
    expect(shouldShowSubscriptionUpgrade(null)).toBe(true);
  });
});
