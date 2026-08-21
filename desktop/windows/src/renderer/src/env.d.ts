/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_OMI_DEPLOYMENT_PROFILE?: 'omi_cloud' | 'self_hosted'
  readonly VITE_OMI_IDENTITY_PROVIDER?: 'firebase' | 'better_auth'
  readonly VITE_OMI_API_BASE?: string
  readonly VITE_OMI_DESKTOP_API_BASE?: string
  readonly VITE_OMI_AUTH_BASE?: string
  readonly VITE_OMI_MCP_BASE?: string
  readonly VITE_OMI_ANALYTICS_BASE?: string
  readonly VITE_OMI_UPDATE_FEED_URL?: string
  readonly VITE_POSTHOG_KEY?: string
  readonly VITE_FIREBASE_API_KEY?: string
  readonly VITE_FIREBASE_AUTH_DOMAIN?: string
  readonly VITE_FIREBASE_PROJECT_ID?: string
  /** Public share origin for conversation links (#4339). Default https://h.omi.me */
  readonly VITE_OMI_SHARE_BASE_URL?: string
}
