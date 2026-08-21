// Ambiguous local timeouts and session cancellation are terminal. Only a typed
// backend response may explicitly authorize replay.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({
  fetch: vi.fn(),
  abortSignal: undefined as AbortSignal | undefined
}))

vi.mock('electron', () => ({ net: { fetch: h.fetch } }))
vi.mock('../core/session', () => ({ getAbortSignal: () => h.abortSignal }))

import { extractMemory } from './gemini'
import type { BackendSession } from '../core/session'

const session = (): BackendSession => ({ apiBase: 'a', desktopApiBase: 'd', token: 't' })
const selfHostedSession = (): BackendSession => ({
  apiBase: 'https://api.operator.test',
  desktopApiBase: 'https://desktop.operator.test',
  token: 'operator-token'
})

function selfHostedProfile(): void {
  vi.stubEnv('VITE_OMI_DEPLOYMENT_PROFILE', 'self_hosted')
  vi.stubEnv('VITE_OMI_IDENTITY_PROVIDER', 'better_auth')
  vi.stubEnv('VITE_OMI_API_BASE', 'https://api.operator.test')
  vi.stubEnv('VITE_OMI_DESKTOP_API_BASE', 'https://desktop.operator.test')
  vi.stubEnv('VITE_OMI_AUTH_BASE', 'https://auth.operator.test')
  vi.stubEnv('VITE_OMI_MCP_BASE', 'https://mcp.operator.test')
}

function capabilityResult(name: string, args: Record<string, unknown>): unknown {
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
  }
}

// A fetch that never resolves on its own — it only rejects when the signal it was
// handed aborts, mirroring real fetch abort semantics (rejects with the reason).
function fetchThatAbortsWithSignal(): void {
  h.fetch.mockImplementation((_url: string, opts: { signal: AbortSignal }) => {
    const s = opts.signal
    return new Promise((_resolve, reject) => {
      const fail = (): void => reject(s.reason ?? new DOMException('aborted', 'AbortError'))
      if (s.aborted) return fail()
      s.addEventListener('abort', fail, { once: true })
    })
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  h.abortSignal = undefined
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
})

describe('extractMemory — retry classification', () => {
  it('routes self-hosted memory through the operator generic capability only', async () => {
    selfHostedProfile()
    h.fetch.mockResolvedValue(
      capabilityResult('submit_memory_extraction', {
        has_new_memory: true,
        memories: [{ content: 'm', category: 'system', source_app: 'App', confidence: 0.9 }],
        context_summary: 's',
        current_activity: 'a'
      })
    )

    await expect(
      extractMemory(selfHostedSession(), 'sys', 'prompt', 'BASE64')
    ).resolves.toMatchObject({ hasNewMemory: true, contextSummary: 's' })
    expect(h.fetch).toHaveBeenCalledTimes(1)
    expect(h.fetch.mock.calls[0][0]).toBe(
      'https://api.operator.test/v1/model-capabilities/tool-completions'
    )
    expect(JSON.stringify(h.fetch.mock.calls)).not.toMatch(/omi\.me|googleapis|proxy\/gemini/i)
  })

  it('does not replay a per-request timeout after dispatch', async () => {
    vi.useFakeTimers()
    h.abortSignal = undefined // no session abort in flight
    fetchThatAbortsWithSignal()

    const promise = extractMemory(session(), 'sys', 'prompt', 'BASE64')
    // Attach the rejection expectation synchronously so the rejection is handled.
    const assertion = expect(promise).rejects.toMatchObject({ name: 'TimeoutError' })

    await vi.advanceTimersByTimeAsync(30_000)

    await assertion
    expect(h.fetch).toHaveBeenCalledTimes(1)
  })

  it('does NOT retry a genuine session sign-out (single attempt, AbortError)', async () => {
    const ctrl = new AbortController()
    ctrl.abort() // the user signed out before the request went out
    h.abortSignal = ctrl.signal
    fetchThatAbortsWithSignal()

    await expect(extractMemory(session(), 'sys', 'prompt', 'BASE64')).rejects.toMatchObject({
      name: 'AbortError'
    })
    expect(h.fetch).toHaveBeenCalledTimes(1)
  })

  it('returns the parsed result on a 200 (single attempt, no retry)', async () => {
    h.fetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        candidates: [
          {
            content: {
              parts: [
                {
                  text: JSON.stringify({
                    has_new_memory: true,
                    memories: [
                      { content: 'm', category: 'system', source_app: 'App', confidence: 0.9 }
                    ],
                    context_summary: 's',
                    current_activity: 'a'
                  })
                }
              ]
            }
          }
        ]
      })
    })
    const r = await extractMemory(session(), 'sys', 'prompt', 'BASE64')
    expect(r?.memories[0]).toEqual({
      content: 'm',
      category: 'system',
      sourceApp: 'App',
      confidence: 0.9
    })
    expect(h.fetch).toHaveBeenCalledTimes(1)
  })
})
