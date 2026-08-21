// Mirrors the macOS desktop app's PostHogManager: best-effort analytics sent to
// the same PostHog project via its HTTP ingestion API (no SDK needed). The project
// key is a publishable client key — safe to embed, exactly as the desktop app
// hardcodes it. Every call is fire-and-forget and never blocks or surfaces errors.
import { auth } from './identity'
import { resolveWindowsDeployment } from '../../../shared/deploymentProfile'

// Host is intentionally fixed to the CSP connect-src allowlist in renderer HTML.
// A VITE_POSTHOG_HOST override would silently fail under Chromium if it diverged.
const deployment = resolveWindowsDeployment()

export function trackEvent(event: string, properties: Record<string, unknown> = {}): void {
  if (!deployment.analyticsBase || !deployment.analyticsKey) return
  const distinctId = auth.currentUser?.uid ?? 'anonymous'
  void fetch(`${deployment.analyticsBase}/i/v0/e/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      api_key: deployment.analyticsKey,
      event,
      distinct_id: distinctId,
      properties: {
        ...properties,
        $lib: 'omi-windows',
        $os: 'Windows',
        platform: 'windows'
      }
    })
  }).catch(() => {
    // Analytics is best-effort — swallow network/auth failures silently.
  })
}

// Same event name + property shape the desktop app's AnalyticsManager sends.
export function trackHowDidYouHear(source: string): void {
  trackEvent('Onboarding How Did You Hear', { source, is_referral: source === 'Friend' })
}
