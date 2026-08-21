import { describe, expect, it, vi } from 'vitest'

vi.mock('electron', () => ({ ipcMain: { handle: vi.fn() }, net: { fetch: vi.fn() } }))

import { runRendererModelCapability, type RendererCapabilityDependencies } from './modelCapability'
import type { BackendSession } from '../assistants/core/session'

const session: BackendSession = {
  apiBase: 'https://operator.example',
  desktopApiBase: 'https://desktop.operator.example',
  token: 'operator-jwt'
}

const route = {
  feature: 'desktop_proactive_reasoning' as const,
  primary: { provider: 'generic', model: 'operator-model' },
  fallbacks: [],
  unavailableFallbacks: []
}

function dependencies(text: string): RendererCapabilityDependencies {
  return {
    deploymentProfile: () => 'self_hosted',
    refreshSession: vi.fn(async () => {}),
    session: () => session,
    signal: () => undefined,
    complete: vi.fn(async () => ({ text, route }))
  }
}

describe('renderer proactive model capability IPC', () => {
  it('owns the screen schema in main and returns operator route metadata', async () => {
    const deps = dependencies('{"candidates":[]}')
    const result = await runRendererModelCapability(
      { surface: 'screen_synthesis', prompt: 'redacted screen context' },
      deps
    )

    expect(deps.complete).toHaveBeenCalledWith(
      expect.objectContaining({
        session,
        prompt: 'redacted screen context',
        responseToolName: 'submit_screen_memories',
        responseSchema: expect.objectContaining({ required: ['candidates'] })
      })
    )
    expect(result.route.primary).toEqual({ provider: 'generic', model: 'operator-model' })
  })

  it('maps the fixed live-note tool result back to plain text', async () => {
    const deps = dependencies('{"note":"  follow up with Alex  "}')
    await expect(
      runRendererModelCapability({ surface: 'live_notes', prompt: 'transcript' }, deps)
    ).resolves.toEqual({ text: 'follow up with Alex', route })
    expect(deps.complete).toHaveBeenCalledWith(
      expect.objectContaining({
        responseToolName: 'submit_live_note',
        maxOutputTokens: 128
      })
    )
  })

  it('fails before the model transport when the identity session is missing', async () => {
    const deps = dependencies('{"candidates":[]}')
    deps.session = () => null
    await expect(
      runRendererModelCapability({ surface: 'screen_synthesis', prompt: 'screen' }, deps)
    ).rejects.toThrow('identity session missing')
    expect(deps.complete).not.toHaveBeenCalled()
  })

  it('rejects arbitrary surfaces and cloud callers before transport', async () => {
    const deps = dependencies('{}')
    await expect(
      runRendererModelCapability({ surface: 'arbitrary_oracle', prompt: 'x' }, deps)
    ).rejects.toThrow('invalid model capability request')
    deps.deploymentProfile = () => 'omi_cloud'
    await expect(
      runRendererModelCapability({ surface: 'live_notes', prompt: 'x' }, deps)
    ).rejects.toThrow('only for self_hosted')
    expect(deps.complete).not.toHaveBeenCalled()
  })
})
