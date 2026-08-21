import { describe, expect, it, vi } from 'vitest'

vi.mock('electron', () => ({ net: { fetch: vi.fn() } }))

import {
  completeStructuredCapability,
  ModelCapabilityUnavailableError
} from './modelCapabilityClient'
import type { BackendSession } from './session'

const session: BackendSession = {
  apiBase: 'https://api.operator.test',
  desktopApiBase: 'https://desktop.operator.test',
  token: 'operator-token'
}

function success(name: string, args: Record<string, unknown>): Response {
  return {
    ok: true,
    json: async () => ({
      status: 'ok',
      capability: 'proactive_tools',
      outcome: 'tool_calls',
      message: {
        role: 'assistant',
        content: '',
        tool_calls: [
          { id: 'call-1', type: 'function', function: { name, arguments: JSON.stringify(args) } }
        ]
      },
      route: {
        feature: 'desktop_proactive_reasoning',
        primary: { provider: 'generic', model: 'operator-model' },
        fallbacks: [],
        unavailable_fallbacks: []
      }
    })
  } as Response
}

describe('model capability client', () => {
  it('uses only the configured backend and returns typed route metadata', async () => {
    const fetchImpl = vi.fn(
      async (_input: string | URL | globalThis.Request, _init?: RequestInit) =>
        success('submit_result', { result: 'ok' })
    )
    const result = await completeStructuredCapability({
      session,
      systemPrompt: 'system',
      prompt: 'inspect',
      imageBase64: 'IMAGE',
      responseToolName: 'submit_result',
      responseSchema: { type: 'object', properties: { result: { type: 'string' } } },
      fetchImpl
    })

    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(fetchImpl.mock.calls[0][0]).toBe(
      'https://api.operator.test/v1/model-capabilities/tool-completions'
    )
    expect(JSON.stringify(fetchImpl.mock.calls)).not.toMatch(/omi\.me|googleapis|openai|anthropic/i)
    expect(JSON.parse(result.text)).toEqual({ result: 'ok' })
    expect(result.route.primary).toEqual({ provider: 'generic', model: 'operator-model' })
  })

  it('rejects undeclared tools before a caller can execute them', async () => {
    await expect(
      completeStructuredCapability({
        session,
        systemPrompt: 'system',
        prompt: 'inspect',
        responseToolName: 'submit_result',
        responseSchema: { type: 'object', properties: {} },
        fetchImpl: vi.fn(async () => success('unexpected_network_tool', {}))
      })
    ).rejects.toThrow('undeclared tool call')
  })

  it('surfaces backend capability absence as a bounded typed error', async () => {
    const fetchImpl = vi.fn(
      async () =>
        ({
          ok: false,
          status: 503,
          json: async () => ({ reason: 'no_configured_route', retryable: false })
        }) as Response
    )

    const promise = completeStructuredCapability({
      session,
      systemPrompt: 'system',
      prompt: 'inspect',
      responseToolName: 'submit_result',
      responseSchema: { type: 'object', properties: {} },
      fetchImpl
    })
    await expect(promise).rejects.toMatchObject({
      name: 'ModelCapabilityUnavailableError',
      status: 503,
      reason: 'no_configured_route',
      retryable: false
    } satisfies Partial<ModelCapabilityUnavailableError>)
  })
})
