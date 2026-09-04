// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
const ports = vi.hoisted(() => ({
  authenticate: vi.fn(async () => {}),
  problem: vi.fn(() => null)
}))
vi.mock('../../renderer/identity', () => ({
  authenticate: ports.authenticate,
  identityProblem: ports.problem
}))
import { Login } from '../../renderer/Login'
import { IdentityError } from '../../native/contract'
import { profile } from '../../native/profile.generated'
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})
it('submits the actual email form, shows canonical identity errors, and offers no unconfigured provider', async () => {
  ports.authenticate.mockRejectedValueOnce(new IdentityError('unavailable'))
  render(<Login />)
  expect(screen.getByText(profile.displayName)).toBeTruthy()
  expect(screen.queryByRole('button', { name: /Google|Apple/ })).toBeNull()
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'fixture@example.invalid' } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'synthetic-password' } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in', exact: true }))
  await waitFor(() =>
    expect(screen.getByRole('alert').textContent).toBe(
      'The identity service is unavailable. Please try again.'
    )
  )
  expect(ports.authenticate).toHaveBeenCalledWith({
    email: 'fixture@example.invalid',
    password: 'synthetic-password'
  })
})
