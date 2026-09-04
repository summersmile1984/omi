'use client';

import { useEffect } from 'react';
import { useRouter } from '@tschk/moonshine-next/navigation';
import { useAuth } from '@/lib/fork/auth-context';
import { LoginForm } from '@/components/fork/LoginForm';

export function LoginClient() {
  const { user, loading } = useAuth();
  const router = useRouter();
  useEffect(() => {
    if (user && !loading) router.push('/home');
  }, [user, loading, router]);
  return (
    <main className="flex min-h-screen items-center justify-center bg-black p-6">
      {loading ? (
        <p role="status" className="text-neutral-300">
          Checking your session…
        </p>
      ) : user ? null : (
        <LoginForm onComplete={() => router.push('/home')} />
      )}
    </main>
  );
}
