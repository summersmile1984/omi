import { createServer } from 'node:http'
import { once } from 'node:events'
import { expect, it } from 'vitest'
import { nativeIdentityFetch } from '../native/http'
import { IdentityClient } from '../native/client'

it('executes the native HTTP identity request without browser metadata and refuses redirect/oversized replies', async () => {
  const requests: string[] = []
  let mode = 'normal'
  const server = createServer((request, response) => {
    requests.push(request.url!)
    expect(request.headers.origin).toBeUndefined()
    expect(request.headers['sec-fetch-mode']).toBeUndefined()
    expect(Number(request.headers['content-length'])).toBeGreaterThan(0)
    expect(request.headers['transfer-encoding']).toBeUndefined()
    if (mode === 'redirect') {
      response.writeHead(302, { Location: '/must-not-follow' })
      response.end()
      return
    }
    if (mode === 'oversized') {
      response.end(Buffer.alloc(1024 * 1024 + 1))
      return
    }
    response.setHeader('set-auth-token', 'opaque-local-http')
    response.end(
      JSON.stringify({ user: { id: 'local', email: 'local@example.invalid', name: 'Local' } })
    )
  })
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  const port = (server.address() as { port: number }).port
  const client = new IdentityClient(`http://127.0.0.1:${port}`, nativeIdentityFetch)
  try {
    expect(
      (await client.authenticate({ email: 'local@example.invalid', password: 'fixture-password' }))
        .session
    ).toBe('opaque-local-http')
    mode = 'redirect'
    await expect(
      client.authenticate({ email: 'local@example.invalid', password: 'fixture-password' })
    ).rejects.toMatchObject({ code: 'unavailable' })
    expect(requests).not.toContain('/must-not-follow')
    mode = 'oversized'
    await expect(
      client.authenticate({ email: 'local@example.invalid', password: 'fixture-password' })
    ).rejects.toMatchObject({ code: 'unavailable' })
    const controller = new AbortController()
    controller.abort()
    await expect(
      nativeIdentityFetch(`http://127.0.0.1:${port}`, {
        redirect: 'error',
        signal: controller.signal
      })
    ).rejects.toThrow()
  } finally {
    server.closeAllConnections()
    server.close()
    await once(server, 'close')
  }
})
