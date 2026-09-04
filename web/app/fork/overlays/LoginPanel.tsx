'use client';

import { useRouter } from '@tschk/moonshine-next/navigation';
import { LoginForm } from '@/components/fork/LoginForm';

export function LoginPanel({
  isOpen,
  onClose,
}: {
  isOpen: boolean;
  onClose: () => void;
}) {
  const router = useRouter();
  if (!isOpen) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-6">
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Sign in"
        className="w-full max-w-md rounded-xl border border-neutral-800 bg-black p-8"
      >
        <button
          type="button"
          aria-label="Close sign in"
          onClick={onClose}
          className="mb-6 text-sm text-neutral-400 underline"
        >
          Close
        </button>
        <LoginForm
          onComplete={() => {
            onClose();
            router.push('/home');
          }}
        />
      </div>
    </div>
  );
}
