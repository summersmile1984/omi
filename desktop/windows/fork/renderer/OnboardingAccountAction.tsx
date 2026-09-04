import { useState } from 'react'
import { signOutUser } from './identity'

// A fresh account can leave the wizard without granting capture permissions or
// completing unrelated integrations. It uses the same revoke/cleanup owner.
export function OnboardingAccountAction(): React.JSX.Element {
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  return (
    <div className="absolute right-8 top-12 z-50 text-right text-sm text-white">
      <button
        className="btn-ghost"
        disabled={busy}
        onClick={async () => {
          setBusy(true)
          setError(null)
          try {
            await signOutUser()
          } catch (cause) {
            setError((cause as Error).message)
          } finally {
            setBusy(false)
          }
        }}
      >
        Sign out
      </button>
      {error && (
        <p role="alert" className="max-w-sm text-red-300">
          {error}
        </p>
      )}
    </div>
  )
}
