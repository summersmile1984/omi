import { WAVE_MAX_SLOTS } from '../../src/renderer/src/orb/waveform'

export const ORB_VERT = `#version 300 es
layout(location = 0) in vec2 a_pos;
void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }
`

// Keep the production frame's genesis/failure pose and actual speech bars. The
// logo-to-waveform handoff replaces the upstream eight-dot geometry; it does not
// manufacture audio, change the speech gate, or schedule an animation loop.
export const ORB_FRAG = `#version 300 es
precision highp float;
uniform vec2 u_resolution;
uniform sampler2D u_brand;
uniform float u_brandRotation;
uniform float u_genesis;
uniform float u_poseOffset;
uniform float u_barMix;
uniform int u_waveCount;
uniform vec4 u_wave[${WAVE_MAX_SLOTS}];
out vec4 outColor;

float sdRoundBox(vec2 p, vec2 b, float r) {
  vec2 q = abs(p) - b + r;
  return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}
void main() {
  vec2 halfSize = 0.5 * u_resolution;
  vec2 q = (gl_FragCoord.xy - halfSize) / min(halfSize.x, halfSize.y);
  q.y = -q.y;
  q /= max(u_genesis, 0.0001);
  q.x -= u_poseOffset;
  float c = cos(u_brandRotation), s = sin(u_brandRotation);
  vec2 uv = (mat2(c, -s, s, c) * q + 1.0) * 0.5;
  float inside = step(0.0, uv.x) * step(uv.x, 1.0) * step(0.0, uv.y) * step(uv.y, 1.0);
  vec4 logo = texture(u_brand, uv);
  float gate = smoothstep(0.0, 0.02, u_genesis);
  float logoA = logo.a * inside * gate * (1.0 - u_barMix);
  float wave = 1e5;
  for (int i = 0; i < ${WAVE_MAX_SLOTS}; i++) {
    if (i >= u_waveCount) break;
    vec4 bar = u_wave[i];
    wave = min(wave, sdRoundBox(q - vec2(bar.x, 0.0), vec2(bar.y, bar.z), min(bar.y, bar.z)));
  }
  float aa = fwidth(wave) + 0.0001;
  float waveA = (1.0 - smoothstep(-aa, aa, wave)) * u_barMix * gate;
  vec3 logoPre = logo.rgb * inside * gate * (1.0 - u_barMix);
  outColor = vec4(vec3(waveA) + logoPre * (1.0 - waveA), waveA + logoA * (1.0 - waveA));
}
`
