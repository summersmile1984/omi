import { describe, it, expect } from 'vitest'
import { buildCloudConnectors } from './cloudConnectors'

function rowValue(rows: { label: string; value: string; blank?: boolean }[], label: string) {
  return rows.find((r) => r.label === label)
}

describe('buildCloudConnectors (field correctness — Mac parity)', () => {
  it('Claude card: omi-claude-prod, blank secret, add-custom-connector URL', () => {
    const [claude] = buildCloudConnectors('https://api.omi.me', {
      claude: 'omi-claude-prod'
    })
    expect(claude.id).toBe('claude')
    expect(claude.connectorUrl).toBe(
      'https://claude.ai/customize/connectors?modal=add-custom-connector'
    )
    expect(rowValue(claude.rows, 'Name')?.value).toBe('Omi Memory')
    expect(rowValue(claude.rows, 'Server URL')?.value).toBe('https://api.omi.me/v1/mcp/sse')
    expect(rowValue(claude.rows, 'OAuth Client ID')?.value).toBe('omi-claude-prod')
    expect(rowValue(claude.rows, 'OAuth Client Secret')?.blank).toBe(true)
  })

  it('ChatGPT card: omi-chatgpt-prod, token_auth_method none, authorize/token URLs', () => {
    const [chatgpt] = buildCloudConnectors('https://api.omi.me', {
      chatgpt: 'omi-chatgpt-prod'
    })
    expect(chatgpt.id).toBe('chatgpt')
    expect(chatgpt.connectorUrl).toBe('https://chatgpt.com/#settings/Connectors')
    expect(rowValue(chatgpt.rows, 'OAuth Client ID')?.value).toBe('omi-chatgpt-prod')
    expect(rowValue(chatgpt.rows, 'Client Secret')?.blank).toBe(true)
    expect(rowValue(chatgpt.rows, 'Token auth method')?.value).toBe('none')
    expect(rowValue(chatgpt.rows, 'Authorization URL')?.value).toBe('https://api.omi.me/authorize')
    expect(rowValue(chatgpt.rows, 'Token URL')?.value).toBe('https://api.omi.me/token')
  })

  it('uses only configured operator ids on a self-hosted base', () => {
    const connectors = buildCloudConnectors('https://mcp.operator.example', {
      chatgpt: 'operator-chatgpt-public'
    })
    expect(connectors).toHaveLength(1)
    expect(connectors[0].id).toBe('chatgpt')
    expect(connectors[0].rows.find((r) => r.label === 'OAuth Client ID')?.value).toBe(
      'operator-chatgpt-public'
    )
    expect(JSON.stringify(connectors)).not.toContain('omi-chatgpt')
    expect(JSON.stringify(connectors)).not.toContain('omi-claude')
  })

  it('omits public connector cards when the self-hosted profile has no registered client', () => {
    expect(buildCloudConnectors('https://mcp.operator.example', {})).toEqual([])
  })
})
