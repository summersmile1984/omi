import { describe, expect, it, vi } from 'vitest'
import { HubController } from './hubController'

describe('HubController deployment boundary', () => {
  it('rejects self-hosted direct realtime before provider, mint, or session construction', async () => {
    const resolveProvider = vi.fn(() => 'gemini' as const)
    const mintToken = vi.fn(async () => 'vendor-token')
    const createSession = vi.fn()
    const controller = new HubController({
      allowsDirectModelProviders: () => false,
      resolveProvider,
      mintToken,
      createSession
    })

    await expect(controller.ensureWarm()).rejects.toThrow(/configured backend relay capability/)
    expect(resolveProvider).not.toHaveBeenCalled()
    expect(mintToken).not.toHaveBeenCalled()
    expect(createSession).not.toHaveBeenCalled()
  })

  it('self-hosted relay chooses the frame adapter from backend wire_protocol without vendor minting', async () => {
    const resolveProvider = vi.fn(() => 'gemini' as const)
    const mintToken = vi.fn(async () => 'vendor-token')
    const createBackendRelay = vi.fn(async () => ({
      connectionId: 'relay-1',
      wireProtocol: 'openai_realtime_v1' as const
    }))
    const createSession = vi.fn((spec) => ({
      provider: spec.provider,
      requiredInputSampleRate: 24000,
      bargeInStrategy: 'inSessionCancel' as const,
      ensureWarm: async () => spec.events.onConnected?.('session-1' as never),
      isWarm: () => true,
      beginTurn: vi.fn(),
      appendAudio: vi.fn(),
      commitTurn: vi.fn(),
      cancelTurn: vi.fn(),
      sendToolResult: vi.fn(),
      teardown: vi.fn()
    }))
    const controller = new HubController({
      allowsDirectModelProviders: () => false,
      resolveProvider,
      mintToken,
      createBackendRelay,
      createSession
    })

    await controller.ensureWarm()

    expect(createBackendRelay).toHaveBeenCalledOnce()
    expect(resolveProvider).not.toHaveBeenCalled()
    expect(mintToken).not.toHaveBeenCalled()
    expect(createSession).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: 'openai',
        token: 'relay-1',
        backendRelayConnectionId: 'relay-1',
        socketFactory: expect.any(Function)
      })
    )
  })
})
