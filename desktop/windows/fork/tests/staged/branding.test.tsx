// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
vi.mock('../../renderer/identity', () => ({ auth: { currentUser: null } }))
import { profile } from '../../native/profile.generated'
import { AboutTab } from '../../renderer/AboutTab'
import { NameStep } from '../../renderer/NameStep'
import { SettingsSearchProvider } from '../../../src/renderer/src/components/settings/SettingsSearchProvider'
import { HowDidYouHearStep } from '../../../src/renderer/src/components/onboarding/HowDidYouHearStep'
import { TrustStep } from '../../../src/renderer/src/components/onboarding/TrustStep'
import { BackgroundConsentControls } from '../../../src/renderer/src/components/consent/BackgroundConsentControls'
import { HubAskBar } from '../../../src/renderer/src/components/home/hub/HubAskBar'
import { HubHeader } from '../../../src/renderer/src/components/home/hub/HubHeader'
import { describeTray, buildTrayMenuTemplate } from '../../../src/main/trayState'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it('renders actual onboarding, persona and tray presentation without changing action ownership', async () => {
  const changeListening = vi.fn(),
    next = vi.fn(),
    show = vi.fn(),
    quit = vi.fn()
  window.omi = {
    getLoginItemSettings: async () => ({ supported: false })
  } as unknown as typeof window.omi
  render(
    <>
      <NameStep stepIndex={0} totalSteps={14} initialValue="Fixture" onContinue={async () => {}} />
      <HowDidYouHearStep stepIndex={2} totalSteps={14} onBack={() => {}} onContinue={next} />
      <TrustStep stepIndex={3} totalSteps={14} onBack={() => {}} onContinue={next} />
      <BackgroundConsentControls
        listening={false}
        onListeningChange={changeListening}
        launchAtLogin={false}
        onLaunchAtLoginChange={() => {}}
      />
      <HubAskBar
        value=""
        onChange={() => {}}
        onSubmit={() => {}}
        onFocus={() => {}}
        sending={false}
        connectActive={false}
        onToggleConnect={() => {}}
      />
    </>
  )
  expect(
    screen.getByRole('heading', { name: `How did you hear about ${profile.displayName}?` })
  ).toBeTruthy()
  expect(
    screen.getByRole('heading', { name: `What should ${profile.personaName} call you?` })
  ).toBeTruthy()
  expect(
    screen
      .getByRole('textbox', { name: `Ask ${profile.personaName} anything` })
      .getAttribute('placeholder')
  ).toBe(`Ask ${profile.personaName} anything`)
  await waitFor(() =>
    expect(screen.getByRole('switch', { name: 'Launch at login' }).hasAttribute('disabled')).toBe(
      true
    )
  )
  expect(document.body.textContent).toContain(`${profile.displayName} stays in your system tray`)
  fireEvent.click(screen.getByRole('switch', { name: 'Continuous listening' }))
  expect(changeListening).toHaveBeenCalledWith(true)
  const open = vi.spyOn(window, 'open').mockImplementation(() => null)
  fireEvent.click(screen.getByRole('button', { name: 'Read documentation' }))
  expect(open).toHaveBeenCalledWith(profile.links.docs)
  expect(document.body.textContent).not.toMatch(/\bomi\b/i)
  expect(describeTray('listening').tooltip).toBe(`${profile.displayName} — listening`)
  const menu = buildTrayMenuTemplate(
    { toggleLabel: 'Resume listening', screenCaptureEnabled: false },
    {
      showMainWindow: show,
      quit,
      toggleListening: vi.fn(),
      openSettings: vi.fn(),
      checkForUpdates: vi.fn(),
      toggleScreenCapture: vi.fn()
    }
  )
  expect(menu.some((item) => /Updates/.test(item.label ?? ''))).toBe(false)
  for (const label of [`Open ${profile.displayName}`, `Quit ${profile.displayName}`]) {
    const click = menu.find((item) => item.label === label)?.click as () => void
    click()
  }
  expect(show).toHaveBeenCalledOnce()
  expect(quit).toHaveBeenCalledOnce()
})

it('renders canonical About destinations and reports unavailable version without enabling disabled updates', async () => {
  window.omi = {
    getAppVersion: vi.fn().mockRejectedValue(new Error('ipc unavailable'))
  } as unknown as typeof window.omi
  render(
    <SettingsSearchProvider>
      <AboutTab />
    </SettingsSearchProvider>
  )
  await screen.findByText('Version unavailable')
  expect(screen.getByText(profile.displayName)).toBeTruthy()
  for (const [label, key] of [
    ['Privacy policy', 'privacy'],
    ['Terms of service', 'terms'],
    ['Open web app', 'webApp'],
    ['Help center', 'help']
  ]) {
    expect(screen.getByRole('link', { name: label }).getAttribute('href')).toBe(
      profile.links[key as keyof typeof profile.links]
    )
  }
  expect(screen.getByRole('link', { name: 'Contact support' }).getAttribute('href')).toBe(
    `mailto:${profile.supportEmail}`
  )
  expect(!!screen.queryByRole('link', { name: 'Community' })).toBe(!!profile.links.community)
  expect(screen.getByText('Automatic updates are disabled for this local test build.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: /update/i })).toBeNull()
  expect(screen.queryByRole('switch', { name: /beta/i })).toBeNull()
  expect(document.body.textContent).not.toMatch(/\bomi\b/i)
})

it('uses the current Home menu owner for feedback and configured community links', async () => {
  const openExternalUrl = vi.fn()
  window.omi = {
    rewindGetSettings: async () => ({ captureEnabled: false }),
    onRewindSettings: () => () => {},
    openExternalUrl
  } as unknown as typeof window.omi
  render(
    <MemoryRouter>
      <HubHeader />
    </MemoryRouter>
  )
  fireEvent.keyDown(screen.getByRole('button', { name: 'Home menu' }), { key: 'ArrowDown' })
  const feedback = await screen.findByRole('menuitem', { name: 'Send feedback' })
  expect(!!screen.queryByRole('menuitem', { name: 'Community' })).toBe(!!profile.links.community)
  expect(screen.queryByRole('menuitem', { name: 'Refer a Friend' })).toBeNull()
  fireEvent.click(feedback)
  expect(openExternalUrl).toHaveBeenCalledWith(profile.links.feedback)
})
