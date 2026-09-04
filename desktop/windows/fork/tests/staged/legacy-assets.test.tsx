// @vitest-environment jsdom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { LegacyHome } from '../../../src/renderer/src/pages/LegacyHome'
import darkLogo from '../../assets/logo_dark.png'
vi.mock('../../renderer/identity', () => ({
  auth: { currentUser: null },
  onAuthStateChanged: () => () => {}
}))
vi.mock('../../../src/renderer/src/components/Markdown', () => ({
  Markdown: ({ text }: { text: string }) => <span>{text}</span>
}))
vi.mock('../../../src/renderer/src/components/home/QuickTaskWidget', () => ({
  QuickTaskWidget: () => null
}))
vi.mock('../../../src/renderer/src/components/home/QuickGoalsWidget', () => ({
  QuickGoalsWidget: () => null
}))
vi.mock('../../../src/renderer/src/components/voice/VoiceSessionSurface', () => ({
  VoiceSessionSurface: () => null
}))
vi.mock('../../../src/renderer/src/state/appState', () => ({
  useAppState: () => ({
    chat: {
      history: [{ id: 'proof', role: 'assistant', content: 'Selected brand reply' }],
      sending: false,
      speaking: false,
      agentActive: false,
      send: vi.fn(),
      reset: vi.fn()
    }
  })
}))
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})
it('uses the selected dark mark on the existing white, clipped assistant badge', () => {
  vi.useFakeTimers()
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  )
  const { container } = render(<LegacyHome />)
  act(() => vi.advanceTimersByTime(1150))
  const image = container.querySelector('img')!
  expect(image.getAttribute('src')).toBe(darkLogo)
  expect(image.parentElement!.className).toContain('bg-white')
  expect(image.parentElement!.className).toContain('overflow-hidden')
  expect(image.getAttribute('alt')).toBe('')
})
