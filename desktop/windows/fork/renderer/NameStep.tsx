import { useState } from 'react'
import { StepScaffold } from '../../src/renderer/src/components/onboarding/StepScaffold'
import { profile } from '../native/profile.generated'

export function NameStep({
  stepIndex,
  totalSteps,
  initialValue,
  onContinue,
  onBack
}: {
  stepIndex: number
  totalSteps: number
  initialValue: string
  onContinue(name: string): Promise<void>
  onBack?: () => void
}): React.JSX.Element {
  const [name, setName] = useState(initialValue)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const submit = async (): Promise<void> => {
    if (busy || !name.trim()) return
    setBusy(true)
    setError(null)
    try {
      await onContinue(name.trim())
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <StepScaffold
      stepIndex={stepIndex}
      totalSteps={totalSteps}
      eyebrow="NAME"
      title={`What should ${profile.displayName} call you?`}
      continueDisabled={busy || !name.trim()}
      onContinue={() => void submit()}
      onBack={onBack}
    >
      <input
        aria-label="Your name"
        value={name}
        onChange={(event) => setName(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') void submit()
        }}
        className="glass-subtle w-64 rounded-lg px-4 py-3 text-center text-sm text-white/90"
      />
      {error && (
        <p role="alert" className="mt-3 text-red-300">
          {error}
        </p>
      )}
    </StepScaffold>
  )
}
