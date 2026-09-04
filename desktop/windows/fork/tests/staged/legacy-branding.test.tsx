// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { LegacyHome } from '../../../src/renderer/src/pages/LegacyHome'
import { Sidebar } from '../../../src/renderer/src/components/layout/Sidebar'
import { profile } from '../../native/profile.generated'
vi.mock('../../renderer/identity', () => ({
  auth: { currentUser: null },
  onAuthStateChanged: () => () => {}
}))
vi.mock('../../../src/renderer/src/components/Markdown', () => ({ Markdown: () => null }))
vi.mock('../../../src/renderer/src/components/home/QuickTaskWidget', () => ({
  QuickTaskWidget: () => null
}))
vi.mock('../../../src/renderer/src/components/home/QuickGoalsWidget', () => ({
  QuickGoalsWidget: () => null
}))
vi.mock('../../../src/renderer/src/components/voice/VoiceSessionSurface', () => ({
  VoiceSessionSurface: () => null
}))
vi.mock('../../../src/renderer/src/components/orb/Orb', () => ({ Orb: () => null }))
vi.mock('../../../src/renderer/src/state/appState', () => ({
  useAppState: () => ({
    chat: {
      history: [{ id: 'pending', role: 'assistant', content: '' }],
      sending: true,
      speaking: false,
      agentActive: false,
      send: vi.fn(),
      reset: vi.fn()
    }
  })
}))
beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  )
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  localStorage.clear()
})
it('projects the persona in actual legacy ask, voice and pending-reply surfaces', () => {
  render(<LegacyHome />)
  act(() => vi.advanceTimersByTime(1150))
  expect(screen.getByPlaceholderText(`Ask ${profile.personaName}…`)).toBeTruthy()
  expect(screen.getByLabelText(`${profile.personaName} is replying`)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: `Talk with ${profile.personaName}` }))
  expect(screen.getByRole('button', { name: 'Hide voice session' })).toBeTruthy()
})
it('renders the product wordmark while retaining an already-saved sidebar preference', () => {
  localStorage.setItem('omi.sidebar.collapsed', '1')
  window.omi = {
    rewindGetSettings: async () => null,
    onRewindSettings: () => () => {}
  } as unknown as typeof window.omi
  render(
    <MemoryRouter>
      <Sidebar />
    </MemoryRouter>
  )
  expect(screen.getByText(profile.displayName)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Expand sidebar' }))
  expect(localStorage.getItem('omi.sidebar.collapsed')).toBe('0')
  expect(screen.getByRole('button', { name: 'Collapse sidebar' })).toBeTruthy()
})
