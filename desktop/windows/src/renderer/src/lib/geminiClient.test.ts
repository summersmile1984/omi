import { beforeEach, describe, expect, it, vi } from 'vitest'

const harness = vi.hoisted(() => ({ profile: 'self_hosted' as 'self_hosted' | 'omi_cloud' }))
vi.mock('../../../shared/deploymentProfile', () => ({
  resolveWindowsDeployment: () => ({ profile: harness.profile })
}))
vi.mock('./identity', () => ({
  auth: { currentUser: { getIdToken: vi.fn(async () => 'cloud-token') } }
}))

import { generate } from './geminiClient'

const capability = vi.fn()

beforeEach(() => {
  vi.restoreAllMocks()
  harness.profile = 'self_hosted'
  capability.mockReset()
  Object.assign(globalThis, { window: { omi: { modelCapabilityGenerate: capability } } })
})

describe('renderer proactive generation deployment routing', () => {
  it('routes screen synthesis through typed main IPC with zero renderer network', async () => {
    capability.mockResolvedValue({ text: '{"candidates":[]}', route: {} })
    const network = vi
      .spyOn(globalThis, 'fetch')
      .mockRejectedValue(new Error('legacy network used'))

    await expect(
      generate({
        model: 'ignored-in-selfhost',
        parts: [{ text: 'redacted screen prompt' }],
        responseSchema: { type: 'object' }
      })
    ).resolves.toBe('{"candidates":[]}')
    expect(capability).toHaveBeenCalledWith({
      surface: 'screen_synthesis',
      prompt: 'redacted screen prompt'
    })
    expect(network).not.toHaveBeenCalled()
  })

  it('routes live notes through typed main IPC with zero renderer network', async () => {
    capability.mockResolvedValue({ text: 'short note', route: {} })
    const network = vi
      .spyOn(globalThis, 'fetch')
      .mockRejectedValue(new Error('legacy network used'))

    await expect(
      generate({ model: 'ignored-in-selfhost', parts: [{ text: 'transcript prompt' }] })
    ).resolves.toBe('short note')
    expect(capability).toHaveBeenCalledWith({
      surface: 'live_notes',
      prompt: 'transcript prompt'
    })
    expect(network).not.toHaveBeenCalled()
  })

  it('keeps the managed-cloud Gemini proxy path', async () => {
    harness.profile = 'omi_cloud'
    vi.stubEnv('VITE_OMI_DESKTOP_API_BASE', 'https://desktop.omi-cloud.test')
    const network = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(
        new Response(
          JSON.stringify({ candidates: [{ content: { parts: [{ text: 'cloud result' }] } }] }),
          { status: 200 }
        )
      )

    await expect(generate({ model: 'gemini-cloud', parts: [{ text: 'prompt' }] })).resolves.toBe(
      'cloud result'
    )
    expect(capability).not.toHaveBeenCalled()
    expect(network).toHaveBeenCalledWith(
      'https://desktop.omi-cloud.test/v1/proxy/gemini/models/gemini-cloud:generateContent',
      expect.objectContaining({ method: 'POST' })
    )
  })
})
