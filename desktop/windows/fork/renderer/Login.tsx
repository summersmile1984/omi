import { useRef, useState } from 'react'
import { authenticate, identityProblem } from './identity'
import { profile } from '../native/profile.generated'

export function Login(): React.JSX.Element {
  const [register, setRegister] = useState(false)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(identityProblem)
  const [waiting, setWaiting] = useState(false)
  const attempt = useRef(0)
  return (
    <main className="app-canvas flex h-full items-center justify-center p-8">
      <form
        className="relative z-10 flex w-full max-w-sm flex-col gap-4 text-white"
        onSubmit={async (event) => {
          event.preventDefault()
          const current = ++attempt.current
          setError(null)
          setWaiting(true)
          try {
            await authenticate({ email, password, ...(register ? { name } : {}) })
          } catch (cause) {
            if (current === attempt.current) setError((cause as Error).message)
          } finally {
            if (current === attempt.current) setWaiting(false)
          }
        }}
      >
        <h1 className="text-2xl font-semibold">{profile.displayName}</h1>
        <p>{register ? 'Create an account' : 'Sign in to continue'}</p>
        {register && (
          <label>
            Name
            <input
              className="mt-1 w-full rounded border border-white/30 bg-black p-3"
              autoComplete="name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </label>
        )}
        <label>
          Email
          <input
            className="mt-1 w-full rounded border border-white/30 bg-black p-3"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </label>
        <label>
          Password
          <input
            className="mt-1 w-full rounded border border-white/30 bg-black p-3"
            type="password"
            autoComplete={register ? 'new-password' : 'current-password'}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={8}
          />
        </label>
        <button
          className="rounded bg-white p-3 font-medium text-black"
          type="submit"
          disabled={waiting}
        >
          {waiting ? 'Please wait…' : register ? 'Create account' : 'Sign in'}
        </button>
        <button
          className="p-2 underline"
          type="button"
          disabled={waiting}
          onClick={() => {
            setRegister(!register)
            setError(null)
          }}
        >
          {register ? 'Use an existing account' : 'Create an account'}
        </button>
        {error && (
          <p role="alert" className="text-red-300">
            {error}
          </p>
        )}
      </form>
    </main>
  )
}
