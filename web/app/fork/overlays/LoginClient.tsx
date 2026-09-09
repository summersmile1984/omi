'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter, useSearchParams } from '@tschk/moonshine-next/navigation';
import { useAuth } from '@/lib/fork/auth-context';
import { LoginForm } from '@/components/fork/LoginForm';
import {
  claimReferral,
  desktopDownloadUrl,
  navigateToDesktopDownload,
} from '@/lib/fork/referrals';

export function LoginClient() {
  const { user, loading, getToken } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const code = params.get('referral');
  const identity = `${user?.uid ?? ''}\0${code ?? ''}`;
  const attempt = useRef({ identity, running: false });
  if (attempt.current.identity !== identity)
    attempt.current = { identity, running: false };
  const [completed, setCompleted] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const finish = useCallback(async () => {
    const current = attempt.current;
    if (!code || current.running) return;
    current.running = true;
    setFailure(null);
    try {
      const result = await claimReferral(code, getToken);
      if (attempt.current !== current) return;
      if (result.claimed) {
        setCompleted(identity);
        navigateToDesktopDownload();
      } else setFailure('This free month is only available to new accounts.');
    } catch (error) {
      if (attempt.current !== current) return;
      setFailure(
        error instanceof Error
          ? error.message
          : 'Unable to apply this referral. Please try again.',
      );
    }
    // A failed attempt stays visible until the user explicitly retries.
  }, [code, getToken, identity]);
  useEffect(() => {
    if (!user || loading) return;
    if (code) void finish();
    else router.push('/home');
  }, [user, loading, router, code, finish]);
  return (
    <main className="flex min-h-screen items-center justify-center bg-black p-6">
      {user && code && completed === identity ? (
        <section className="max-w-sm space-y-4 text-white">
          <h1 className="text-2xl font-semibold">Your 30-day Operator trial is ready.</h1>
          <p>If your download did not start, use the link below.</p>
          <a className="block underline" href={desktopDownloadUrl()}>
            Download the desktop app
          </a>
          <button className="block underline" onClick={() => router.push('/home')}>
            Continue to your account
          </button>
        </section>
      ) : user && code && failure ? (
        <section className="max-w-sm space-y-4 text-white">
          <p role="alert">{failure}</p>
          <button
            className="rounded-lg bg-white px-4 py-2 text-black"
            onClick={() => {
              attempt.current.running = false;
              void finish();
            }}
          >
            Retry referral
          </button>
          <button className="block underline" onClick={() => router.push('/home')}>
            Continue to your account
          </button>
        </section>
      ) : loading || (user && code) ? (
        <p role="status" className="text-neutral-300">
          {user && code ? 'Applying your referral…' : 'Checking your session…'}
        </p>
      ) : user ? null : (
        <LoginForm
          onComplete={() => {
            if (!code) router.push('/home');
          }}
        />
      )}
    </main>
  );
}
