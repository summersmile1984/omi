import { brandTexture } from './brand-texture.generated'

// The validated, bounded RGBA texture is decoded at build time. Constructor-time
// GL upload adds no Image/fetch await and belongs to the existing renderer owner.
export function installBrandTexture(
  gl: WebGL2RenderingContext,
  program: WebGLProgram
): WebGLTexture {
  const data = Uint8Array.from(atob(brandTexture.rgba), (value) => value.charCodeAt(0))
  if (
    brandTexture.width !== 64 ||
    brandTexture.height !== 64 ||
    !brandTexture.premultiplied ||
    data.length !== 64 * 64 * 4
  )
    throw new Error('Invalid generated brand texture')
  const texture = gl.createTexture()
  if (!texture) throw new Error('Brand texture allocation failed')
  try {
    gl.activeTexture(gl.TEXTURE0)
    gl.bindTexture(gl.TEXTURE_2D, texture)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 64, 64, 0, gl.RGBA, gl.UNSIGNED_BYTE, data)
    gl.uniform1i(gl.getUniformLocation(program, 'u_brand'), 0)
    if (gl.getError() !== gl.NO_ERROR) throw new Error('Brand texture upload failed')
    return texture
  } catch (error) {
    gl.deleteTexture(texture)
    throw error
  }
}
