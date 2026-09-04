// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
const graph = vi.hoisted(() => ({
  suspended: false,
  ready: false,
  pending: Promise.resolve(),
  finish: () => {}
}))
vi.mock('../../../src/renderer/src/components/graph/BrainGraph', () => ({
  BrainGraph: () => {
    if (graph.suspended && !graph.ready) throw graph.pending
    return <span>Graph ready</span>
  }
}))
vi.mock('../../renderer/identity', () => ({ auth: { currentUser: { displayName: 'Synthetic' } } }))
vi.mock('../../renderer/OnboardingAccountAction', () => ({ OnboardingAccountAction: () => null }))
vi.mock('../../renderer/NameStep', () => ({ NameStep: () => null }))
vi.mock('../../../src/renderer/src/components/onboarding/TrustStep', () => ({
  TrustStep: () => null
}))
vi.mock('../../../src/renderer/src/components/onboarding/BackgroundPrivacyStep', () => ({
  BackgroundPrivacyStep: () => null
}))
vi.mock('../../../src/renderer/src/components/onboarding/ScreenPermissionStep', () => ({
  ScreenPermissionStep: () => null
}))
vi.mock('../../../src/renderer/src/components/onboarding/BuildProfileStep', () => ({
  BuildProfileStep: () => null
}))
vi.mock('../../../src/renderer/src/components/onboarding/MicPermissionStep', () => ({
  MicPermissionStep: () => null
}))
vi.mock('../../../src/renderer/src/components/onboarding/AutomationPermissionStep', () => ({
  AutomationPermissionStep: () => null
}))
vi.mock('../../../src/renderer/src/components/onboarding/ShortcutSetupStep', () => ({
  ShortcutSetupStep: () => null
}))
vi.mock('../../../src/renderer/src/components/onboarding/VoiceIntroStep', () => ({
  VoiceIntroStep: () => null
}))
vi.mock('../../../src/renderer/src/components/onboarding/AskDemoStep', () => ({
  AskDemoStep: () => null
}))
vi.mock('../../../src/renderer/src/components/onboarding/DataSourcesStep', () => ({
  DataSourcesStep: () => null
}))
vi.mock('../../../src/renderer/src/components/onboarding/GoalStep', () => ({
  GoalStep: () => null
}))
vi.mock('../../../src/renderer/src/lib/userProfile', () => ({
  syncLanguage: async () => {},
  setDisplayName: async () => {}
}))
vi.mock('../../../src/renderer/src/lib/goals', () => ({ createGoal: async () => {} }))
vi.mock('../../../src/renderer/src/lib/analytics', () => ({ trackHowDidYouHear: () => {} }))
vi.mock('../../../src/renderer/src/lib/toast', () => ({ toast: () => {} }))
import { Onboarding } from '../../../src/renderer/src/pages/Onboarding'
import { getPreferences, setPreferences } from '../../../src/renderer/src/lib/preferences'
afterEach(cleanup)
it('commits language progress while graph fonts suspend, and renders the real graph after readiness', async () => {
  graph.suspended = false
  graph.ready = false
  graph.pending = new Promise<void>((resolve) => {
    graph.finish = resolve
  })
  window.omi = {
    localGraphLoad: async () => ({ nodes: [{ id: 'user', label: 'Synthetic' }], edges: [] }),
    localGraphUpsert: async (nodes: unknown[], edges: unknown[]) => {
      graph.suspended = true
      return { nodes, edges }
    }
  } as unknown as typeof window.omi
  setPreferences({ onboardingStep: 1, displayName: 'Synthetic' })
  render(<Onboarding />)
  await screen.findByText('Graph ready')
  fireEvent.click(screen.getByRole('button', { name: 'English', exact: true }))
  await waitFor(() =>
    expect(screen.getByRole('heading', { name: 'How did you hear about Omi?' })).toBeTruthy()
  )
  expect(getPreferences().onboardingStep).toBe(2)
  expect(screen.getByRole('status').textContent).toContain('You can continue setup')
  await act(async () => {
    graph.ready = true
    graph.finish()
    await graph.pending
  })
  expect(screen.getByText('Graph ready')).toBeTruthy()
  expect(screen.queryByRole('status')).toBeNull()
})
