import { dirname, relative } from 'node:path'

export function applyAssetConsumers({ read, write, replaceOnce, removeFunction }) {
  function modulePath(path, target) {
    const value = relative(dirname(path), target).replaceAll('\\', '/')
    return value.startsWith('.') ? value : './' + value
  }
  replaceOnce(
    'src/main/index.ts',
    "'../../resources/icon.png?asset'",
    "'../../resources/fork/icon.png?asset'"
  )
  for (const state of ['idle', 'listening', 'paused'])
    replaceOnce(
      'src/main/tray.ts',
      `'../../resources/tray/${state}.ico?asset'`,
      `'../../resources/fork/tray/${state}.png?asset'`
    )
  for (const [path, before] of [
    ['src/renderer/src/components/orb/Orb.tsx', '../../assets/omi-logo.png']
  ])
    replaceOnce(
      path,
      JSON.stringify(before).replaceAll('"', "'"),
      JSON.stringify(modulePath(path, 'fork/assets/logo_light.png'))
    )
  const legacy = 'src/renderer/src/pages/LegacyHome.tsx'
  replaceOnce(
    legacy,
    "import omiMark from '../assets/omi-mark.png'",
    `import { BrandMark } from ${JSON.stringify(modulePath(legacy, 'fork/renderer/BrandMark'))}`
  )
  replaceOnce(legacy, "import { BrandImage } from '../components/ui/BrandImage'", '')
  replaceOnce(
    legacy,
    '<BrandImage src={omiMark} alt="Omi" className="h-14 w-14 object-contain" />',
    '<BrandMark lightSurface className="h-14 w-14 object-contain" />'
  )

  const connector = 'src/renderer/src/components/home/hub/connections/ConnectorBrandMark.tsx'
  removeFunction(connector, 'OmiMark')
  write(
    connector,
    `import { BrandMark as OmiMark } from ${JSON.stringify(modulePath(connector, 'fork/renderer/BrandMark'))}\n` +
      read(connector)
  )

  const renderer = 'src/renderer/src/orb/orbRenderer.ts'
  replaceOnce(
    renderer,
    "import { ORB_VERT, ORB_FRAG } from './shader'",
    `import { ORB_VERT, ORB_FRAG } from ${JSON.stringify(modulePath(renderer, 'fork/renderer/brandShader'))}\nimport { installBrandTexture } from ${JSON.stringify(modulePath(renderer, 'fork/renderer/brandTexture'))}`
  )
  replaceOnce(
    renderer,
    '  private disposed = false',
    '  private disposed = false\n  private brandTexture: WebGLTexture'
  )
  replaceOnce(renderer, "      'u_poseOffset',", "      'u_poseOffset',\n      'u_brandRotation',")
  replaceOnce(
    renderer,
    '      this.u[name] = gl.getUniformLocation(program, name)\n    }',
    `      this.u[name] = gl.getUniformLocation(program, name)
    }
    try { this.brandTexture = installBrandTexture(gl, program) }
    catch (error) { gl.deleteBuffer(buf); gl.deleteVertexArray(vao); gl.deleteProgram(program); gl.getExtension('WEBGL_lose_context')?.loseContext(); throw error }`
  )
  replaceOnce(
    renderer,
    '    gl.uniform1f(this.u.u_poseOffset, frame.poseOffsetX)',
    '    gl.uniform1f(this.u.u_poseOffset, frame.poseOffsetX)\n    gl.uniform1f(this.u.u_brandRotation, Math.atan2(frame.dots[0].y, frame.dots[0].x))'
  )
  replaceOnce(
    renderer,
    '    this.disposed = true',
    '    this.disposed = true\n    this.gl.deleteTexture(this.brandTexture)'
  )
}
