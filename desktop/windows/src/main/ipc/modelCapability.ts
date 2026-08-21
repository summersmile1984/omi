import { ipcMain } from 'electron'
import type {
  RendererModelCapabilityRequest,
  RendererModelCapabilityResult
} from '../../shared/types'
import { resolveWindowsDeployment } from '../../shared/deploymentProfile'
import {
  completeStructuredCapability,
  type CapabilityRoute
} from '../assistants/core/modelCapabilityClient'
import {
  getAbortSignal,
  getBackendSession,
  pullFreshSession,
  type BackendSession
} from '../assistants/core/session'

const SCREEN_RESPONSE_SCHEMA = {
  type: 'object',
  properties: {
    candidates: {
      type: 'array',
      items: {
        type: 'object',
        properties: { text: { type: 'string' }, confidence: { type: 'number' } },
        required: ['text', 'confidence']
      }
    }
  },
  required: ['candidates']
}

const LIVE_NOTE_SCHEMA = {
  type: 'object',
  properties: { note: { type: 'string' } },
  required: ['note']
}

const LIVE_NOTE_SYSTEM_PROMPT =
  'You are a concise note-taker. Generate a single short note (3-10 words) about the key point in the transcript. Do not use quotes. Be direct and specific.'
const MAX_PROMPT_CHARS = 16_000

type CompleteStructured = (opts: {
  session: BackendSession
  systemPrompt: string
  prompt: string
  responseToolName: string
  responseSchema: Record<string, unknown>
  maxOutputTokens: number
  signal?: AbortSignal
}) => Promise<{ text: string; route: CapabilityRoute }>

export type RendererCapabilityDependencies = {
  deploymentProfile: () => string
  refreshSession: () => Promise<void>
  session: () => BackendSession | null
  signal: () => AbortSignal | undefined
  complete: CompleteStructured
}

const productionDependencies: RendererCapabilityDependencies = {
  deploymentProfile: () => resolveWindowsDeployment().profile,
  refreshSession: pullFreshSession,
  session: getBackendSession,
  signal: getAbortSignal,
  complete: completeStructuredCapability
}

function parseRequest(value: unknown): RendererModelCapabilityRequest {
  if (!value || typeof value !== 'object') throw new Error('invalid model capability request')
  const raw = value as Record<string, unknown>
  if (
    (raw.surface !== 'screen_synthesis' && raw.surface !== 'live_notes') ||
    typeof raw.prompt !== 'string' ||
    raw.prompt.length === 0 ||
    raw.prompt.length > MAX_PROMPT_CHARS
  ) {
    throw new Error('invalid model capability request')
  }
  return { surface: raw.surface, prompt: raw.prompt }
}

export async function runRendererModelCapability(
  value: unknown,
  deps: RendererCapabilityDependencies = productionDependencies
): Promise<RendererModelCapabilityResult> {
  if (deps.deploymentProfile() !== 'self_hosted') {
    throw new Error('renderer model capability IPC is available only for self_hosted')
  }
  const input = parseRequest(value)
  await deps.refreshSession()
  const session = deps.session()
  if (!session) throw new Error('model capability unavailable: identity session missing')

  const result = await deps.complete({
    session,
    prompt: input.prompt,
    systemPrompt:
      input.surface === 'live_notes'
        ? LIVE_NOTE_SYSTEM_PROMPT
        : 'Extract only durable facts explicitly supported by the supplied screen text.',
    responseToolName:
      input.surface === 'live_notes' ? 'submit_live_note' : 'submit_screen_memories',
    responseSchema: input.surface === 'live_notes' ? LIVE_NOTE_SCHEMA : SCREEN_RESPONSE_SCHEMA,
    maxOutputTokens: input.surface === 'live_notes' ? 128 : 1024,
    signal: deps.signal()
  })
  if (input.surface === 'live_notes') {
    let note: unknown
    try {
      note = (JSON.parse(result.text) as { note?: unknown }).note
    } catch {
      throw new Error('model capability returned an invalid live note')
    }
    if (typeof note !== 'string') throw new Error('model capability returned an invalid live note')
    return { text: note.trim(), route: result.route }
  }
  return result
}

let registered = false
export function registerRendererModelCapabilityHandlers(): void {
  if (registered) return
  registered = true
  ipcMain.handle('modelCapability:generate', (_event, value: unknown) =>
    runRendererModelCapability(value)
  )
}

export function _resetRendererModelCapabilityHandlersForTests(): void {
  registered = false
}
