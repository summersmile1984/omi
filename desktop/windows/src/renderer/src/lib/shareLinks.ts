/** Public share-link base URL for self-hosting (#4339). Matches backend OMI_SHARE_BASE_URL. */

import { resolveWindowsDeployment } from '../../../shared/deploymentProfile'

const DEFAULT_SHARE_BASE = 'https://h.omi.me'

export function shareBaseUrl(
  raw: string | undefined = import.meta.env.VITE_OMI_SHARE_BASE_URL as string | undefined
): string {
  const deployment = resolveWindowsDeployment()
  let value = (raw ?? '').trim()
  if (!value) value = deployment.shareBase ?? ''
  if (!value) throw new Error('Public conversation sharing is disabled by this deployment profile')
  if (!value.includes('://')) value = `https://${value}`
  try {
    const parsed = new URL(value)
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) {
      if (deployment.profile === 'self_hosted') throw new Error('Invalid self-hosted share origin')
      return DEFAULT_SHARE_BASE
    }
  } catch {
    if (deployment.profile === 'self_hosted') throw new Error('Invalid self-hosted share origin')
    return DEFAULT_SHARE_BASE
  }
  return value.replace(/\/+$/, '')
}

export function conversationShareUrl(
  id: string,
  raw?: string | undefined
): string {
  return `${shareBaseUrl(raw)}/conversations/${id}`
}
