// Embedding transport for main-process Rewind/task indexing. Self-hosted builds
// use the provider-neutral Python capability with an opaque identity bearer and
// dynamic projection identity. Managed cloud retains its legacy Gemini proxy.
//
// This lives in main (not the renderer) because the indexer it feeds is a
// background job that must survive renderer navigation/reloads — same reason as
// `ipc/memoryCleanup.ts`. The identity bearer is relayed over IPC (see
// `embeddingService.configureRewindEmbedSession`).
import { net } from 'electron'
import { EMBED_DIM, EMBED_MODEL, l2Normalize } from './embedVector'

/** Gemini task types. Asymmetric on purpose: a stored passage and a search query
 *  are embedded into the same space but with different intent, which measurably
 *  improves retrieval over using one type for both. */
export type EmbedTaskType = 'RETRIEVAL_DOCUMENT' | 'RETRIEVAL_QUERY'

/** Where to reach the proxy, and who is asking. Relayed from the renderer. */
export type EmbedSession = { apiBase?: string; desktopApiBase: string; token: string }

export type EmbeddingPurpose = 'ocr' | 'task' | 'rewind'
export type EmbeddingMode = 'document' | 'query'
export type EmbeddingProjection = {
  provider: string
  model: string
  dimension: number
  schemaVersion: number
  namespaceVersion: string
  logicalNamespace: string
}
export type CapabilityEmbeddingResult = {
  vectors: (Float32Array | null)[]
  projection: EmbeddingProjection
  responseGeneration: number
}

const responseGenerationBySurface = { task: 0, rewind: 0 }

function issueProjectionResponseGeneration(purpose: EmbeddingPurpose): number {
  const surface = purpose === 'task' ? 'task' : 'rewind'
  responseGenerationBySurface[surface] += 1
  return responseGenerationBySurface[surface]
}

/** Commits projection responses in request-generation order. A response older
 * than the last successful projection switch may reuse that exact marker, but
 * can never reactivate a different projection. Failed newer responses do not
 * advance the fence, so an older successful request can still make progress. */
export class EmbeddingProjectionResponseFence {
  private committedGeneration = 0
  private committedProjectionKey: string | null = null

  commit(
    responseGeneration: number,
    projectionKey: string,
    markerMatches: () => boolean,
    activate: () => boolean
  ): boolean {
    if (responseGeneration < this.committedGeneration) {
      if (projectionKey === this.committedProjectionKey && markerMatches()) return false
      throw new Error('stale embedding projection response')
    }
    const invalidated = activate()
    this.committedGeneration = responseGeneration
    this.committedProjectionKey = projectionKey
    return invalidated
  }
}

type CapabilityFetch = (
  input: string,
  init?: RequestInit & { bypassCustomProtocolHandlers?: boolean }
) => Promise<Response>

const REQUEST_TIMEOUT_MS = 30_000
const MAX_RETRIES = 2

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

type EmbeddingResponse = { embedding?: { values?: number[] } }
type BatchEmbeddingResponse = { embeddings?: { values?: number[] }[] }

type CapabilityEmbeddingResponse = {
  status?: unknown
  capability?: unknown
  data?: { index?: unknown; embedding?: unknown }[]
  projection?: {
    provider?: unknown
    model?: unknown
    dimension?: unknown
    schema_version?: unknown
    namespace_version?: unknown
    logical_namespace?: unknown
  }
}

/** Body of one `embedContent` request (also the element shape of a batch). */
function requestBody(text: string, taskType: EmbedTaskType): Record<string, unknown> {
  return {
    model: `models/${EMBED_MODEL}`,
    content: { parts: [{ text }] },
    taskType
  }
}

/** Validate + normalize one raw `values` array from the API. */
function toVector(values: number[] | undefined): Float32Array | null {
  if (!values || values.length !== EMBED_DIM) return null
  return l2Normalize(Float32Array.from(values))
}

function normalizeCapabilityVector(values: number[]): Float32Array {
  const converted = Float32Array.from(values)
  if (!Array.from(converted).every(Number.isFinite)) {
    throw new Error('embedding capability vector overflows client precision')
  }
  const normSquared = converted.reduce((sum, value) => sum + value * value, 0)
  if (!Number.isFinite(normSquared) || normSquared <= 0) {
    throw new Error('embedding capability returned a zero or invalid vector norm')
  }
  const normalized = l2Normalize(converted)
  if (!Array.from(normalized).every(Number.isFinite)) {
    throw new Error('embedding capability returned an invalid normalized vector')
  }
  return normalized
}

function projectionFromWire(value: CapabilityEmbeddingResponse['projection']): EmbeddingProjection {
  if (
    !value ||
    typeof value.provider !== 'string' ||
    typeof value.model !== 'string' ||
    !Number.isInteger(value.dimension) ||
    (value.dimension as number) <= 0 ||
    !Number.isInteger(value.schema_version) ||
    typeof value.namespace_version !== 'string' ||
    typeof value.logical_namespace !== 'string'
  ) {
    throw new Error('embedding capability returned an invalid projection identity')
  }
  return {
    provider: value.provider,
    model: value.model,
    dimension: value.dimension as number,
    schemaVersion: value.schema_version as number,
    namespaceVersion: value.namespace_version,
    logicalNamespace: value.logical_namespace
  }
}

export function embeddingProjectionKey(projection: EmbeddingProjection): string {
  return [
    projection.provider,
    projection.model,
    projection.dimension,
    projection.schemaVersion,
    projection.namespaceVersion,
    projection.logicalNamespace
  ].join('|')
}

/** Provider-neutral embedding transport used by self-hosted builds. The caller
 * supplies the logical namespace explicitly; no provider/model is selected in
 * the client. Response order is reconstructed from the bounded wire `index`. */
export async function embedCapabilityBatch(
  session: EmbedSession,
  texts: string[],
  purpose: EmbeddingPurpose,
  mode: EmbeddingMode,
  projectionNamespace: string,
  fetchImpl: CapabilityFetch = net.fetch
): Promise<CapabilityEmbeddingResult> {
  if (!session.apiBase) throw new Error('embedding capability backend origin is not configured')
  if (texts.length === 0 || texts.length > 32) {
    throw new Error('embedding capability accepts 1-32 inputs')
  }
  const responseGeneration = issueProjectionResponseGeneration(purpose)
  const response = await fetchImpl(`${session.apiBase}/v1/model-capabilities/embeddings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${session.token}` },
    body: JSON.stringify({ purpose, mode, input: texts, projection_namespace: projectionNamespace })
  })
  if (!response.ok) {
    throw new Error(`embedding capability request failed (status ${response.status})`)
  }
  const payload = (await response.json()) as CapabilityEmbeddingResponse
  if (
    payload.status !== 'ok' ||
    payload.capability !== 'embedding' ||
    !Array.isArray(payload.data)
  ) {
    throw new Error('embedding capability returned an invalid envelope')
  }
  const projection = projectionFromWire(payload.projection)
  if (projection.logicalNamespace !== projectionNamespace) {
    throw new Error('embedding capability returned the wrong logical namespace')
  }
  if (payload.data.length !== texts.length) {
    throw new Error('embedding capability returned the wrong result count')
  }
  const vectors: (Float32Array | null)[] = Array.from({ length: texts.length }, () => null)
  for (const item of payload.data) {
    if (
      !Number.isInteger(item.index) ||
      (item.index as number) < 0 ||
      (item.index as number) >= texts.length
    ) {
      throw new Error('embedding capability returned an invalid result index')
    }
    if (vectors[item.index as number] !== null) {
      throw new Error('embedding capability returned a duplicate result index')
    }
    if (!Array.isArray(item.embedding) || item.embedding.length !== projection.dimension) {
      throw new Error('embedding capability returned a vector outside its projection dimension')
    }
    const values = item.embedding
    if (!values.every((value) => typeof value === 'number' && Number.isFinite(value))) {
      throw new Error('embedding capability returned a non-finite vector')
    }
    vectors[item.index as number] = normalizeCapabilityVector(values as number[])
  }
  if (vectors.some((vector) => vector === null)) {
    throw new Error('embedding capability omitted an input vector')
  }
  return { vectors, projection, responseGeneration }
}

/**
 * POST to the proxy with retries on 429/503 (the same policy as the renderer's
 * Gemini client). Errors are deliberately sanitized to a status code: the proxy
 * body can echo request content and auth material, and this text ends up in logs.
 */
async function post(
  session: EmbedSession,
  method: string,
  body: unknown
): Promise<Record<string, unknown>> {
  const url = `${session.desktopApiBase}/v1/proxy/gemini/models/${EMBED_MODEL}:${method}`
  let lastError = ''
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS)
    try {
      // Electron's net.fetch uses Chromium's network stack (proxy/TLS aware) —
      // the path the rest of the app's main-process HTTP already takes.
      const res = await net.fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${session.token}` },
        body: JSON.stringify(body),
        signal: ctrl.signal
      })
      if (res.ok) return (await res.json()) as Record<string, unknown>
      if (res.status === 429 || res.status === 503) {
        lastError = `status ${res.status}`
        // No point sleeping after the last attempt — we are about to throw.
        if (attempt < MAX_RETRIES) await sleep(400 * (attempt + 1))
        continue
      }
      throw new Error(`embedding proxy request failed (status ${res.status})`)
    } catch (e) {
      // A timeout/network drop is retryable; a thrown non-retryable status is not.
      if (e instanceof Error && e.message.startsWith('embedding proxy request failed')) throw e
      lastError = `network: ${(e as Error).message}`
      if (attempt === MAX_RETRIES) break
      await sleep(400 * (attempt + 1))
    } finally {
      clearTimeout(timer)
    }
  }
  throw new Error(`embedding proxy request failed after retries (${lastError})`)
}

/** Embed one text (used for search queries). Throws on failure. */
export async function embedOne(
  session: EmbedSession,
  text: string,
  taskType: EmbedTaskType
): Promise<Float32Array> {
  const json = (await post(
    session,
    'embedContent',
    requestBody(text, taskType)
  )) as EmbeddingResponse
  const vec = toVector(json.embedding?.values)
  if (!vec) throw new Error('embedding proxy returned no usable vector')
  return vec
}

/**
 * The provider's hard limit on one `batchEmbedContents` body. Verified live: 101
 * requests is a `400 INVALID_ARGUMENT` that fails the entire batch. Enforced here
 * as well as at the queue (`EMBED_BATCH_SIZE`) so no future caller can put an
 * over-limit body on the wire, whatever it thinks its batch size is.
 */
const MAX_BATCH_REQUESTS = 100

/**
 * Embed a batch of texts. Returns one entry per input, in order; an entry is null
 * when the API omitted it or returned a wrong-dimension vector, so one bad item
 * can't discard the whole batch. Throws only when a request itself fails.
 *
 * Chunked at the API's 100-request ceiling, sequentially — a caller with 250
 * texts gets 3 round trips, not one guaranteed 400.
 */
export async function embedBatch(
  session: EmbedSession,
  texts: string[],
  taskType: EmbedTaskType
): Promise<(Float32Array | null)[]> {
  if (texts.length === 0) return []
  const out: (Float32Array | null)[] = []
  for (let i = 0; i < texts.length; i += MAX_BATCH_REQUESTS) {
    const chunk = texts.slice(i, i + MAX_BATCH_REQUESTS)
    const json = (await post(session, 'batchEmbedContents', {
      requests: chunk.map((t) => requestBody(t, taskType))
    })) as BatchEmbeddingResponse
    const embeddings = json.embeddings ?? []
    for (let j = 0; j < chunk.length; j++) out.push(toVector(embeddings[j]?.values))
  }
  return out
}
