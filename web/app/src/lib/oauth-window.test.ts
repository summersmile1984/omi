import { describe, expect, it, vi } from 'vitest';

import {
  closeOAuthPopup,
  navigateOAuthPopup,
  openOAuthPopup,
  type OAuthPopup,
  type OAuthWindowHost,
} from './oauth-window';

function host(popup: OAuthPopup | null = null): OAuthWindowHost {
  return {
    location: { href: 'https://omi.example/settings' },
    open: vi.fn(() => popup),
  };
}

function popup(): OAuthPopup {
  return { closed: false, location: { href: '' }, close: vi.fn() };
}

describe('OAuth popup lifecycle', () => {
  it('opens synchronously with a blank URL and fixed window features', () => {
    const opened = popup();
    const windowHost = host(opened);

    expect(openOAuthPopup(windowHost)).toBe(opened);
    expect(windowHost.open).toHaveBeenCalledWith('', '_blank', 'width=600,height=700');
  });

  it('navigates the popup and falls back to the current tab when blocked', () => {
    const opened = popup();
    const windowHost = host(opened);
    navigateOAuthPopup(windowHost, opened, 'https://accounts.google.com/auth');
    expect(opened.location.href).toBe('https://accounts.google.com/auth');

    const blockedHost = host(null);
    navigateOAuthPopup(blockedHost, null, 'https://accounts.google.com/auth');
    expect(blockedHost.location.href).toBe('https://accounts.google.com/auth');
  });

  it('closes a pre-opened popup on request failure', () => {
    const opened = popup();
    closeOAuthPopup(opened);
    expect(opened.close).toHaveBeenCalledOnce();
  });
});
