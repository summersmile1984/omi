import { useState } from 'react'
import { User, LogOut } from 'lucide-react'
import { auth, signOutUser, updateProfile } from './identity'
import { SettingRow } from '../../src/renderer/src/components/settings/SettingRow'

export function AccountTab(): React.JSX.Element {
  const [name, setName] = useState(auth.currentUser?.displayName ?? '')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  async function perform(operation: () => Promise<void>): Promise<void> {
    setError(null)
    setBusy(true)
    try {
      await operation()
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      <SettingRow
        icon={User}
        title="Profile"
        subtitle="Your display name."
        keywords="name profile display"
      >
        <div className="space-y-3">
          <input
            aria-label="Your name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="glass-subtle w-full rounded-lg px-4 py-3 text-sm text-text-secondary"
          />
          <button
            className="btn-ghost"
            disabled={busy}
            onClick={() =>
              void perform(async () => {
                const user = auth.currentUser
                if (!user) throw new Error('Please sign in again.')
                await updateProfile(user, { displayName: name.trim() })
              })
            }
          >
            Save
          </button>
        </div>
      </SettingRow>
      <SettingRow
        icon={LogOut}
        title="Signed in"
        subtitle={auth.currentUser?.email ?? '(not signed in)'}
        keywords="account email sign out logout"
        control={
          <button className="btn-ghost" disabled={busy} onClick={() => void perform(signOutUser)}>
            Sign out
          </button>
        }
      />
      {error && (
        <p role="alert" className="text-red-300">
          {error}
        </p>
      )}
    </>
  )
}
