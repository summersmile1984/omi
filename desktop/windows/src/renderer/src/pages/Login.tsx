import { useRef, useState } from 'react'
import { Apple } from 'lucide-react'
import type { SignInProvider } from '../../../shared/types'
import { signInWithBetterAuth, signInWithProvider } from '../lib/identity'
import { resolveWindowsDeployment } from '../../../shared/deploymentProfile'
import omiLogo from '../assets/omilogo.png'
import { BrandImage } from '../components/ui/BrandImage'

export function Login(): React.JSX.Element {
  const selfHosted = resolveWindowsDeployment().profile === 'self_hosted'
  // 'activeProvider' spans the whole system-browser round-trip (opening the browser →
  // loopback callback → token exchange). Success flips auth state globally via
  // onAuthStateChanged, which unmounts this page. (A still-pending attempt's
  // loopback listener in main self-closes on supersede or its 5-min timeout —
  // at most one listener ever exists, and it's gone after success.)
  const [activeProvider, setActiveProvider] = useState<SignInProvider | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [createAccount, setCreateAccount] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  // Per-click generation: only the NEWEST attempt may write error/waiting
  // state, so a late failure from a superseded-era attempt (however phrased)
  // can never clobber the retry's pending UI.
  const attemptRef = useRef(0)

  const onClick = async (provider: SignInProvider): Promise<void> => {
    // No guard on `waiting`: clicking again supersedes the pending attempt in
    // main (closes the stale loopback listener) and starts a fresh one, so a
    // closed browser tab never blocks retrying for the full 5-min timeout.
    const attempt = ++attemptRef.current
    setError(null)
    setActiveProvider(provider)
    try {
      await signInWithProvider(provider)
      // Signed in — onAuthStateChanged takes over; nothing else to do here.
    } catch (e) {
      if (attempt !== attemptRef.current) return // a newer attempt owns the UI
      console.error('Sign-in failed:', e)
      setError((e as Error).message)
      setActiveProvider(null)
    }
  }

  const onBetterAuthSubmit = async (event: React.FormEvent): Promise<void> => {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await signInWithBetterAuth({ email, password, name, createAccount })
    } catch (e) {
      setError((e as Error).message)
      setSubmitting(false)
    }
  }

  return (
    <div className="app-canvas relative flex h-full items-center justify-center p-8">
      <div className="animate-fade-in relative z-10 flex w-full max-w-[420px] flex-col items-center">
        <BrandImage src={omiLogo} alt="omi" className="h-24 w-auto" />
        <p className="mt-6 text-base leading-relaxed text-white/70">Sign in to continue</p>
        {selfHosted ? (
          <form className="mt-10 flex w-full flex-col gap-3" onSubmit={(e) => void onBetterAuthSubmit(e)}>
            {createAccount && (
              <input
                aria-label="Name"
                className="rounded-xl border border-white/15 bg-white/5 px-4 py-3 text-white outline-none focus:border-white/40"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Name"
                autoComplete="name"
                required
              />
            )}
            <input
              aria-label="Email"
              className="rounded-xl border border-white/15 bg-white/5 px-4 py-3 text-white outline-none focus:border-white/40"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="Email"
              type="email"
              autoComplete="email"
              required
            />
            <input
              aria-label="Password"
              className="rounded-xl border border-white/15 bg-white/5 px-4 py-3 text-white outline-none focus:border-white/40"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Password"
              type="password"
              minLength={8}
              autoComplete={createAccount ? 'new-password' : 'current-password'}
              required
            />
            <button
              type="submit"
              className="rounded-xl bg-white px-8 py-3.5 font-medium text-black"
              disabled={submitting}
            >
              {submitting ? 'Signing in…' : createAccount ? 'Create account' : 'Sign in'}
            </button>
            <button
              type="button"
              className="text-sm text-white/60 hover:text-white"
              onClick={() => setCreateAccount((value) => !value)}
            >
              {createAccount ? 'Already have an account? Sign in' : 'Need an account? Create one'}
            </button>
          </form>
        ) : (
          <>
            <div className="h-48" />
        <button
          type="button"
          onClick={() => void onClick('apple')}
          className="flex items-center justify-center gap-3 rounded-xl bg-black px-8 py-3.5 font-medium text-white transition-opacity hover:opacity-90"
        >
          <Apple className="h-5 w-5" strokeWidth={2.5} aria-hidden="true" />
          {activeProvider === 'apple' ? 'Try again' : 'Continue with Apple'}
        </button>
        <button
          type="button"
          onClick={() => void onClick('google')}
          className="mt-3 flex items-center justify-center gap-3 rounded-xl bg-white px-8 py-3.5 font-medium text-black transition-opacity hover:opacity-90"
        >
          <svg viewBox="0 0 48 48" className="h-5 w-5">
            <path
              fill="#EA4335"
              d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
            />
            <path
              fill="#4285F4"
              d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
            />
            <path
              fill="#FBBC05"
              d="M10.54 28.59A14.5 14.5 0 0 1 9.5 24c0-1.59.28-3.14.76-4.59l-7.98-6.19A23.99 23.99 0 0 0 0 24c0 3.77.87 7.35 2.56 10.56l7.98-5.97z"
            />
            <path
              fill="#34A853"
              d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 5.97C6.51 42.62 14.62 48 24 48z"
            />
          </svg>
          {activeProvider === 'google' ? 'Try again' : 'Continue with Google'}
        </button>
          </>
        )}
        <div className="mt-4 min-h-[3rem] text-center">
          {!selfHosted && activeProvider && !error && (
            <p className="animate-fade-in text-sm text-white/50">
              Waiting for your browser&hellip; finish signing in there, then come back.
            </p>
          )}
          {error && <p className="text-sm text-red-400/90">{error}</p>}
        </div>
      </div>
    </div>
  )
}
