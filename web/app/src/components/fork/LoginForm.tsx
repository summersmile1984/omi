'use client';

import { useState, type FormEvent } from 'react';
import { useAuth } from '@/lib/fork/auth-context';
import { productName } from '@/lib/fork/web-profile';

export function LoginForm({ onComplete }: { onComplete: () => void }) {
  const { signInWithEmail, signUpWithEmail, authError, retrySession } = useAuth();
  const [creatingAccount, setCreatingAccount] = useState(false);
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (creatingAccount) await signUpWithEmail(name.trim(), email.trim(), password);
      else await signInWithEmail(email.trim(), password);
      setPassword('');
      onComplete();
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : 'Unable to sign in. Please try again.',
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="w-full max-w-sm space-y-6 text-white">
      <div>
        <p className="mb-2 text-sm text-neutral-400">{productName()}</p>
        <h1 className="text-2xl font-semibold">
          {creatingAccount ? 'Create your account' : 'Sign in'}
        </h1>
      </div>
      {(error || authError) && (
        <div role="alert" className="rounded-lg border border-neutral-700 p-3 text-sm">
          <p>{error || authError}</p>
          {authError && (
            <button
              type="button"
              onClick={() => void retrySession()}
              className="mt-2 underline"
            >
              Retry saved session
            </button>
          )}
        </div>
      )}
      <form onSubmit={submit} className="space-y-4">
        {creatingAccount && (
          <label className="block text-sm">
            Name
            <input
              required
              autoComplete="name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              className="mt-2 w-full rounded-lg border border-neutral-700 bg-neutral-950 p-3"
            />
          </label>
        )}
        <label className="block text-sm">
          Email
          <input
            required
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="mt-2 w-full rounded-lg border border-neutral-700 bg-neutral-950 p-3"
          />
        </label>
        <label className="block text-sm">
          Password
          <input
            required
            type="password"
            minLength={creatingAccount ? 8 : 1}
            autoComplete={creatingAccount ? 'new-password' : 'current-password'}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="mt-2 w-full rounded-lg border border-neutral-700 bg-neutral-950 p-3"
          />
        </label>
        <button
          disabled={busy}
          type="submit"
          className="w-full rounded-lg bg-white p-3 font-medium text-black disabled:opacity-50"
        >
          {busy ? 'Please wait…' : creatingAccount ? 'Create account' : 'Sign in'}
        </button>
      </form>
      <button
        type="button"
        disabled={busy}
        onClick={() => {
          setCreatingAccount(!creatingAccount);
          setError(null);
        }}
        className="text-sm text-neutral-300 underline disabled:opacity-50"
      >
        {creatingAccount ? 'Already have an account? Sign in' : 'Create an account'}
      </button>
    </section>
  );
}
