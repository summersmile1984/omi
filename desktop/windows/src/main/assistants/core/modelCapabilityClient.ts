// Provider-neutral self-hosted proactive-model transport. Managed-cloud callers
// keep their existing Gemini proxy wire; self-hosted callers converge here so a
// signed operator backend, not the client, owns provider/model selection.
import { net } from 'electron'
import type { BackendSession } from './session'

export type CapabilityRoute = {
  feature: 'desktop_proactive_reasoning'
  primary: { provider: string; model: string }
  fallbacks: { provider: string; model: string }[]
  unavailableFallbacks: { provider: string; model: string; reason: string }[]
}

export type CapabilityToolCall = { name: string; args: Record<string, unknown> }
export type CapabilityToolTurn = {
  toolCalls: CapabilityToolCall[]
  text: string
  route: CapabilityRoute
}

export class ModelCapabilityUnavailableError extends Error {
  constructor(
    readonly status: number,
    readonly reason: string,
    readonly retryable: boolean
  ) {
    super(`model capability unavailable (${reason})`)
    this.name = 'ModelCapabilityUnavailableError'
  }
}

type CapabilityFetch = (
  input: string | URL | globalThis.Request,
  init?: RequestInit
) => Promise<Response>

type GeminiPart =
  | { text: string }
  | { inlineData: { mimeType: string; data: string } }
  | { functionCall: { name: string; args: Record<string, unknown> } }
  | { functionResponse: { name: string; response: { result: string } } }
type GeminiContent = { role: 'user' | 'model'; parts: GeminiPart[] }
type GeminiTool = {
  function_declarations: Array<{
    name: string
    description?: string
    parameters: Record<string, unknown>
  }>
}

function asObject(value: unknown, message: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(message)
  return value as Record<string, unknown>
}

function messagesFromGemini(
  systemPrompt: string,
  contents: GeminiContent[]
): Record<string, unknown>[] {
  const messages: Record<string, unknown>[] = [{ role: 'system', content: systemPrompt }]
  let callSequence = 0

  for (const content of contents) {
    if (!Array.isArray(content.parts) || content.parts.length === 0) {
      throw new Error('model capability transcript contains an empty turn')
    }
    if (content.role === 'model') {
      const text = content.parts
        .filter((part): part is { text: string } => 'text' in part)
        .map((part) => part.text)
        .join('')
      const calls = content.parts.filter(
        (part): part is { functionCall: { name: string; args: Record<string, unknown> } } =>
          'functionCall' in part
      )
      const toolCalls = calls.map((part) => {
        const id = `client-call-${++callSequence}`
        return {
          id,
          type: 'function',
          function: {
            name: part.functionCall.name,
            arguments: JSON.stringify(part.functionCall.args)
          }
        }
      })
      messages.push({
        role: 'assistant',
        content: text,
        ...(toolCalls.length ? { tool_calls: toolCalls } : {})
      })
      continue
    }

    const responses = content.parts.filter(
      (part): part is { functionResponse: { name: string; response: { result: string } } } =>
        'functionResponse' in part
    )
    if (responses.length) {
      const previous = messages.at(-1)
      const previousCalls = previous?.role === 'assistant' ? previous.tool_calls : null
      const pendingCall =
        Array.isArray(previousCalls) && previousCalls.length === 1
          ? asObject(previousCalls[0], 'model capability transcript contains an invalid tool call')
          : null
      const pendingFunction = pendingCall
        ? asObject(
            pendingCall.function,
            'model capability transcript contains an invalid tool function'
          )
        : null
      if (
        responses.length !== 1 ||
        content.parts.length !== 1 ||
        !pendingCall ||
        !pendingFunction ||
        typeof pendingCall.id !== 'string' ||
        typeof pendingFunction.name !== 'string'
      ) {
        throw new Error('model capability transcript contains an unmatched tool response')
      }
      const response = responses[0].functionResponse
      if (response.name !== pendingFunction.name) {
        throw new Error('model capability transcript tool response names do not match')
      }
      messages.push({
        role: 'tool',
        tool_call_id: pendingCall.id,
        content: response.response.result
      })
      continue
    }

    const parts = content.parts.map((part) => {
      if ('text' in part) return { type: 'text', text: part.text }
      if ('inlineData' in part) {
        const mime = part.inlineData.mimeType.toLowerCase()
        if (!['image/png', 'image/jpeg', 'image/webp'].includes(mime)) {
          throw new Error('model capability accepts only PNG, JPEG, or WebP inline images')
        }
        return {
          type: 'image_url',
          image_url: { url: `data:${mime};base64,${part.inlineData.data}` }
        }
      }
      throw new Error('model capability user turn contains an unsupported part')
    })
    messages.push({ role: 'user', content: parts })
  }
  return messages
}

function toolsFromGemini(tool: GeminiTool): Record<string, unknown>[] {
  return tool.function_declarations.map((declaration) => ({
    type: 'function',
    function: {
      name: declaration.name,
      description: declaration.description ?? '',
      parameters: declaration.parameters
    }
  }))
}

function parseRoute(raw: unknown): CapabilityRoute {
  const route = asObject(raw, 'model capability returned invalid route metadata')
  const primary = asObject(route.primary, 'model capability returned invalid primary route')
  if (
    route.feature !== 'desktop_proactive_reasoning' ||
    typeof primary.provider !== 'string' ||
    !primary.provider ||
    typeof primary.model !== 'string' ||
    !primary.model
  ) {
    throw new Error('model capability returned invalid route metadata')
  }
  const routeList = (value: unknown, unavailable: boolean): CapabilityRoute['fallbacks'] => {
    if (!Array.isArray(value))
      throw new Error('model capability returned invalid fallback metadata')
    return value.map((entry) => {
      const item = asObject(entry, 'model capability returned invalid fallback route')
      if (typeof item.provider !== 'string' || typeof item.model !== 'string') {
        throw new Error('model capability returned invalid fallback route')
      }
      if (unavailable && typeof item.reason !== 'string') {
        throw new Error('model capability returned invalid unavailable fallback route')
      }
      return {
        provider: item.provider,
        model: item.model,
        ...(unavailable ? { reason: item.reason as string } : {})
      }
    })
  }
  return {
    feature: 'desktop_proactive_reasoning',
    primary: { provider: primary.provider, model: primary.model },
    fallbacks: routeList(route.fallbacks, false),
    unavailableFallbacks: routeList(
      route.unavailable_fallbacks,
      true
    ) as CapabilityRoute['unavailableFallbacks']
  }
}

async function unavailable(response: Response): Promise<ModelCapabilityUnavailableError> {
  let reason = `http_${response.status}`
  let retryable = response.status >= 500
  try {
    const payload = asObject(await response.json(), 'invalid unavailable payload')
    if (typeof payload.reason === 'string' && payload.reason) reason = payload.reason
    if (typeof payload.retryable === 'boolean') retryable = payload.retryable
  } catch {
    // Status-only is the bounded fallback; response bodies may contain provider detail.
  }
  return new ModelCapabilityUnavailableError(response.status, reason, retryable)
}

export async function completeToolCapability(opts: {
  session: BackendSession
  systemPrompt: string
  contents: GeminiContent[]
  tool: GeminiTool
  forceToolCall: boolean
  maxOutputTokens?: number
  signal?: AbortSignal
  fetchImpl?: CapabilityFetch
}): Promise<CapabilityToolTurn> {
  const declaredTools = new Set(opts.tool.function_declarations.map((tool) => tool.name))
  const response = await (opts.fetchImpl ?? net.fetch)(
    `${opts.session.apiBase}/v1/model-capabilities/tool-completions`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${opts.session.token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        messages: messagesFromGemini(opts.systemPrompt, opts.contents),
        tools: toolsFromGemini(opts.tool),
        tool_choice: opts.forceToolCall ? 'required' : 'auto',
        max_output_tokens: opts.maxOutputTokens ?? 1024
      }),
      signal: opts.signal
    }
  )
  if (!response.ok) throw await unavailable(response)
  const payload = asObject(await response.json(), 'model capability returned an invalid envelope')
  if (payload.status !== 'ok' || payload.capability !== 'proactive_tools') {
    throw new Error('model capability returned an invalid envelope')
  }
  const message = asObject(
    payload.message,
    'model capability returned an invalid assistant message'
  )
  if (message.role !== 'assistant' || !Array.isArray(message.tool_calls)) {
    throw new Error('model capability returned an invalid assistant message')
  }
  const toolCalls = message.tool_calls.map((raw) => {
    const call = asObject(raw, 'model capability returned an invalid tool call')
    const fn = asObject(call.function, 'model capability returned an invalid tool function')
    if (
      call.type !== 'function' ||
      typeof fn.name !== 'string' ||
      typeof fn.arguments !== 'string'
    ) {
      throw new Error('model capability returned an invalid tool call')
    }
    if (!declaredTools.has(fn.name)) {
      throw new Error('model capability returned an undeclared tool call')
    }
    const args = asObject(
      JSON.parse(fn.arguments),
      'model capability returned non-object tool arguments'
    )
    return { name: fn.name, args }
  })
  const content = message.content
  const text =
    typeof content === 'string'
      ? content
      : Array.isArray(content)
        ? content
            .map((part) =>
              part && typeof part === 'object' && typeof part.text === 'string' ? part.text : ''
            )
            .join('')
        : ''
  if (payload.outcome !== (toolCalls.length ? 'tool_calls' : 'message')) {
    throw new Error('model capability outcome does not match its assistant message')
  }
  return { toolCalls, text, route: parseRoute(payload.route) }
}

export async function completeStructuredCapability(opts: {
  session: BackendSession
  systemPrompt: string
  prompt: string
  imageBase64?: string
  responseToolName: string
  responseSchema: Record<string, unknown>
  maxOutputTokens?: number
  signal?: AbortSignal
  fetchImpl?: CapabilityFetch
}): Promise<{ text: string; route: CapabilityRoute }> {
  const parts: GeminiPart[] = [{ text: opts.prompt }]
  if (opts.imageBase64)
    parts.push({ inlineData: { mimeType: 'image/jpeg', data: opts.imageBase64 } })
  const turn = await completeToolCapability({
    session: opts.session,
    systemPrompt: opts.systemPrompt,
    contents: [{ role: 'user', parts }],
    tool: {
      function_declarations: [
        {
          name: opts.responseToolName,
          description: 'Return the structured result for this request.',
          parameters: opts.responseSchema
        }
      ]
    },
    forceToolCall: true,
    maxOutputTokens: opts.maxOutputTokens,
    signal: opts.signal,
    fetchImpl: opts.fetchImpl
  })
  if (turn.toolCalls.length !== 1 || turn.toolCalls[0].name !== opts.responseToolName) {
    throw new Error('model capability did not return the required structured result')
  }
  return { text: JSON.stringify(turn.toolCalls[0].args), route: turn.route }
}
