// Cloud (OAuth) MCP connectors — ChatGPT and Claude. These connect to the
// deployment's public MCP endpoint through the PROVIDER's own OAuth flow and
// public PKCE client; they need NO hosted key (secret is left blank). The app can't
// drive the provider's form, so the assisted flow opens the provider's connector
// page and shows a guide card of copy-rows the user pastes in.
//
// Client ids come from the signed deployment profile. Cloud profiles resolve the
// historical public defaults; self-hosted profiles produce no card unless the
// operator explicitly registers and configures that provider's public client.
//
// CONNECTED-STATE: Mac has an UNCLOSED connected-detection gap for cloud
// connectors (its latch is only set by a dead automation path) — we replicate
// that gap rather than invent a probe. The card carries no connected flag; the
// renderer keeps a local "opened" latch so a returning user sees "Reconnect".

import { mcpServerUrl, type McpCloudConnectorInfo } from '../../shared/mcpExports'

function trimBase(apiBase: string): string {
  return apiBase.replace(/\/+$/, '')
}

export type McpCloudOAuthClients = {
  chatgpt?: string
  claude?: string
}

/** Build the ChatGPT + Claude assisted-connector cards for this API base. */
export function buildCloudConnectors(
  apiBase: string,
  clients: McpCloudOAuthClients
): McpCloudConnectorInfo[] {
  const base = trimBase(apiBase)
  const serverUrl = mcpServerUrl(base)
  const connectors: McpCloudConnectorInfo[] = []

  if (clients.claude) {
    connectors.push({
      id: 'claude',
      title: 'Claude',
      connectorUrl: 'https://claude.ai/customize/connectors?modal=add-custom-connector',
      rows: [
        { label: 'Name', value: 'Omi Memory' },
        { label: 'Server URL', value: serverUrl },
        { label: 'OAuth Client ID', value: clients.claude },
        { label: 'OAuth Client Secret', value: '', blank: true }
      ]
    })
  }

  if (clients.chatgpt) {
    connectors.push({
      id: 'chatgpt',
      title: 'ChatGPT',
      connectorUrl: 'https://chatgpt.com/#settings/Connectors',
      rows: [
        { label: 'Name', value: 'Omi Memory' },
        { label: 'Server URL', value: serverUrl },
        { label: 'OAuth Client ID', value: clients.chatgpt },
        { label: 'Client Secret', value: '', blank: true },
        { label: 'Token auth method', value: 'none' },
        { label: 'Authorization URL', value: `${base}/authorize` },
        { label: 'Token URL', value: `${base}/token` }
      ]
    })
  }

  return connectors
}
