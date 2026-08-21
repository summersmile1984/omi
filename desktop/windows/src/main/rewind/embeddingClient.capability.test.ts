import { describe, expect, it, vi } from 'vitest'
import {
  embedCapabilityBatch,
  embeddingProjectionKey,
  EmbeddingProjectionResponseFence
} from './embeddingClient'

const SESSION = {
  apiBase: 'https://operator.example',
  desktopApiBase: 'https://desktop.operator.example',
  token: 'opaque-jwt'
}

describe('provider-neutral embedding capability', () => {
  it('posts the explicit workload/namespace and consumes dynamic projection identity', async () => {
    const fetchImpl = vi.fn(
      async (_input: string, _init?: RequestInit) =>
        new Response(
          JSON.stringify({
            status: 'ok',
            capability: 'embedding',
            data: [
              { index: 0, embedding: [3, 4] },
              { index: 1, embedding: [0, 2] }
            ],
            projection: {
              provider: 'generic',
              model: 'operator-embed',
              dimension: 2,
              schema_version: 7,
              namespace_version: 'v12',
              logical_namespace: 'ns3'
            }
          }),
          { status: 200, headers: { 'content-type': 'application/json' } }
        )
    )

    const result = await embedCapabilityBatch(
      SESSION,
      ['screen one', 'screen two'],
      'rewind',
      'document',
      'ns3',
      fetchImpl
    )

    expect(fetchImpl).toHaveBeenCalledWith(
      'https://operator.example/v1/model-capabilities/embeddings',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ Authorization: 'Bearer opaque-jwt' })
      })
    )
    const requestInit = fetchImpl.mock.calls[0]![1] as RequestInit
    expect(JSON.parse(String(requestInit.body))).toEqual({
      purpose: 'rewind',
      mode: 'document',
      input: ['screen one', 'screen two'],
      projection_namespace: 'ns3'
    })
    expect(Array.from(result.vectors[0]!)).toEqual([0.6000000238418579, 0.800000011920929])
    expect(result.vectors[0]).toHaveLength(2)
    expect(result.responseGeneration).toBeGreaterThan(0)
    expect(embeddingProjectionKey(result.projection)).toBe('generic|operator-embed|2|7|v12|ns3')
  })

  it('rejects a dimension mismatch before returning any vector to storage', async () => {
    const fetchImpl = vi.fn(
      async (_input: string, _init?: RequestInit) =>
        new Response(
          JSON.stringify({
            status: 'ok',
            capability: 'embedding',
            data: [{ index: 0, embedding: [1] }],
            projection: {
              provider: 'generic',
              model: 'operator-embed',
              dimension: 2,
              schema_version: 1,
              namespace_version: 'v1',
              logical_namespace: 'ns4'
            }
          }),
          { status: 200 }
        )
    )

    await expect(
      embedCapabilityBatch(SESSION, ['task'], 'task', 'query', 'ns4', fetchImpl)
    ).rejects.toThrow(/projection dimension/)
  })

  it('rejects a mismatched logical namespace before storage projection activation', async () => {
    const fetchImpl = vi.fn(
      async (_input: string, _init?: RequestInit) =>
        new Response(
          JSON.stringify({
            status: 'ok',
            capability: 'embedding',
            data: [{ index: 0, embedding: [1, 0] }],
            projection: {
              provider: 'generic',
              model: 'operator-embed',
              dimension: 2,
              schema_version: 1,
              namespace_version: 'v1',
              logical_namespace: 'ns3'
            }
          }),
          { status: 200 }
        )
    )

    await expect(
      embedCapabilityBatch(SESSION, ['task'], 'task', 'document', 'ns4', fetchImpl)
    ).rejects.toThrow(/wrong logical namespace/)
  })

  it.each([
    { embedding: [0, 0], message: /zero or invalid vector norm/ },
    { embedding: [1e308, 1], message: /overflows client precision/ }
  ])('rejects $embedding after conversion to client precision', async ({ embedding, message }) => {
    const fetchImpl = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            status: 'ok',
            capability: 'embedding',
            data: [{ index: 0, embedding }],
            projection: {
              provider: 'generic',
              model: 'operator-embed',
              dimension: 2,
              schema_version: 1,
              namespace_version: 'v1',
              logical_namespace: 'ns4'
            }
          }),
          { status: 200 }
        )
    )

    await expect(
      embedCapabilityBatch(SESSION, ['task'], 'task', 'query', 'ns4', fetchImpl)
    ).rejects.toThrow(message)
  })

  it('rejects duplicate/extra result rows instead of overwriting an input vector', async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            status: 'ok',
            capability: 'embedding',
            data: [
              { index: 0, embedding: [1, 0] },
              { index: 0, embedding: [0, 1] }
            ],
            projection: {
              provider: 'generic',
              model: 'operator-embed',
              dimension: 2,
              schema_version: 1,
              namespace_version: 'v1',
              logical_namespace: 'ns4'
            }
          }),
          { status: 200 }
        )
    )

    await expect(
      embedCapabilityBatch(SESSION, ['first', 'second'], 'task', 'document', 'ns4', fetchImpl)
    ).rejects.toThrow(/duplicate result index/)
  })

  it('never rolls a committed projection back when responses arrive out of order', () => {
    const fence = new EmbeddingProjectionResponseFence()
    let marker = 'initial'
    const commit = (generation: number, key: string): boolean =>
      fence.commit(
        generation,
        key,
        () => marker === key,
        () => {
          const changed = marker !== key
          marker = key
          return changed
        }
      )

    expect(commit(2, 'v2')).toBe(true)
    expect(() => commit(1, 'v1')).toThrow(/stale embedding projection response/)
    expect(marker).toBe('v2')
  })

  it('allows ordered switches and does not let a failed newer response block an older success', () => {
    const ordered = new EmbeddingProjectionResponseFence()
    let marker = 'initial'
    const activate = (key: string): boolean => {
      const changed = marker !== key
      marker = key
      return changed
    }
    expect(
      ordered.commit(
        1,
        'v1',
        () => marker === 'v1',
        () => activate('v1')
      )
    ).toBe(true)
    expect(
      ordered.commit(
        2,
        'v2',
        () => marker === 'v2',
        () => activate('v2')
      )
    ).toBe(true)
    expect(marker).toBe('v2')

    const failedNewer = new EmbeddingProjectionResponseFence()
    marker = 'initial'
    expect(() =>
      failedNewer.commit(
        2,
        'v2',
        () => false,
        () => {
          throw new Error('backend failure')
        }
      )
    ).toThrow(/backend failure/)
    expect(
      failedNewer.commit(
        1,
        'v1',
        () => false,
        () => activate('v1')
      )
    ).toBe(true)
    expect(marker).toBe('v1')
  })
})
