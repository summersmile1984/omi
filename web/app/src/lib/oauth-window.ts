export type OAuthPopup = {
  closed: boolean;
  location: { href: string };
  close: () => void;
};

export type OAuthWindowHost = {
  location: { href: string };
  open: (url?: string, target?: string, features?: string) => OAuthPopup | null;
};

/** Open the popup while the click gesture is still active. */
export function openOAuthPopup(host: OAuthWindowHost): OAuthPopup | null {
  return host.open('', '_blank', 'width=600,height=700');
}

/** Put the provider URL in the pre-opened popup, with a current-tab fallback. */
export function navigateOAuthPopup(
  host: OAuthWindowHost,
  popup: OAuthPopup | null,
  authUrl: string,
) {
  if (popup && !popup.closed) {
    popup.location.href = authUrl;
  } else {
    host.location.href = authUrl;
  }
}

export function closeOAuthPopup(popup: OAuthPopup | null) {
  if (popup && !popup.closed) popup.close();
}
