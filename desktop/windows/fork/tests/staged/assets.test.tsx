// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ConnectorBrandMark } from '../../../src/renderer/src/components/home/hub/connections/ConnectorBrandMark'
import { OmiThinkingSpinner } from '../../renderer/BrandThinkingSpinner'
import { OrbRenderer } from '../../../src/renderer/src/orb/orbRenderer'
import { computeOrbFrame } from '../../../src/renderer/src/orb/choreography'
import { profile } from '../../native/profile.generated'
import lightLogo from '../../assets/logo_light.png'
afterEach(cleanup)
it('renders selected inline marks and retains the existing image recovery path', () => {
  const { container } = render(
    <>
      <ConnectorBrandMark brand="omi" />
      <OmiThinkingSpinner />
    </>
  )
  const images = container.querySelectorAll('img')
  expect(images.length).toBe(2)
  for (const image of images) expect(image.getAttribute('src')).toBe(lightLogo)
  expect(screen.getByRole('status').getAttribute('aria-label')).toBe(
    `${profile.personaName} is thinking`
  )
  fireEvent.error(images[0])
  expect(container.querySelector('img')?.getAttribute('src')).toBe(`${lightLogo}?r=1`)
  fireEvent.error(container.querySelector('img')!)
  expect(container.querySelectorAll('img').length).toBe(1)
  expect(container.querySelectorAll('svg circle').length).toBe(1)
})
function graphics() {
  const gl: any = {
    NO_ERROR: 0,
    TEXTURE0: 1,
    TEXTURE_2D: 2,
    RGBA: 3,
    UNSIGNED_BYTE: 4,
    ARRAY_BUFFER: 5,
    STATIC_DRAW: 6,
    FLOAT: 7,
    TRIANGLES: 8,
    COLOR_BUFFER_BIT: 9,
    VERTEX_SHADER: 10,
    FRAGMENT_SHADER: 11,
    COMPILE_STATUS: 12,
    LINK_STATUS: 13,
    LINEAR: 14,
    CLAMP_TO_EDGE: 15
  }
  for (const name of [
    'shaderSource',
    'compileShader',
    'attachShader',
    'linkProgram',
    'bindVertexArray',
    'bindBuffer',
    'bufferData',
    'enableVertexAttribArray',
    'vertexAttribPointer',
    'useProgram',
    'activeTexture',
    'bindTexture',
    'texParameteri',
    'texImage2D',
    'uniform1i',
    'uniform1f',
    'uniform2f',
    'uniform4fv',
    'uniform1fv',
    'viewport',
    'clearColor',
    'clear',
    'drawArrays',
    'deleteTexture',
    'deleteProgram',
    'deleteVertexArray',
    'deleteBuffer'
  ])
    gl[name] = vi.fn()
  for (const name of [
    'createShader',
    'createProgram',
    'createVertexArray',
    'createBuffer',
    'createTexture'
  ])
    gl[name] = vi.fn(() => ({}))
  gl.getShaderParameter = () => true
  gl.getProgramParameter = () => true
  gl.getUniformLocation = (_program: unknown, name: string) => name
  gl.getError = () => 0
  const loseContext = vi.fn()
  gl.getExtension = () => ({ loseContext })
  return {
    gl,
    loseContext,
    canvas: { width: 64, height: 64, getContext: () => gl } as unknown as HTMLCanvasElement
  }
}
it('uploads bounded premultiplied texture in the production renderer and keeps frame/resource ownership', () => {
  const { gl, loseContext, canvas } = graphics(),
    renderer = new OrbRenderer(canvas)
  const upload = gl.texImage2D.mock.calls[0]
  expect(upload.slice(3, 5)).toEqual([64, 64])
  expect(upload[8].length).toBe(64 * 64 * 4)
  for (let i = 0; i < upload[8].length; i += 4)
    for (let c = 0; c < 3; c++) expect(upload[8][i + c]).toBeLessThanOrEqual(upload[8][i + 3])
  renderer.render(computeOrbFrame({ t: 0, state: 'idle', stateTime: 3 }))
  expect(gl.uniform1f).toHaveBeenCalledWith('u_brandRotation', 0)
  const frame = computeOrbFrame({
    t: 1,
    state: 'speaking',
    stateTime: 1,
    amplitude: 0.6,
    speechMerge: 1,
    waveLevels: [0.2, 0.6, 0.3]
  })
  renderer.render(frame)
  expect(gl.uniform1f).toHaveBeenCalledWith('u_barMix', frame.barMix)
  expect(gl.uniform1i).toHaveBeenCalledWith('u_waveCount', frame.waveBars.length)
  expect(gl.drawArrays).toHaveBeenCalledTimes(2)
  renderer.dispose()
  renderer.dispose()
  expect(gl.deleteTexture).toHaveBeenCalledOnce()
  expect(loseContext).toHaveBeenCalledOnce()
  renderer.render(frame)
  expect(gl.drawArrays).toHaveBeenCalledTimes(2)
})
it('releases texture and constructor resources when upload fails', () => {
  const { gl, loseContext, canvas } = graphics()
  gl.texImage2D.mockImplementation(() => {
    throw Error('upload failed')
  })
  expect(() => new OrbRenderer(canvas)).toThrow('upload failed')
  for (const name of ['deleteTexture', 'deleteProgram', 'deleteVertexArray', 'deleteBuffer'])
    expect(gl[name]).toHaveBeenCalledOnce()
  expect(loseContext).toHaveBeenCalledOnce()
})
