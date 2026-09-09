'use client';

import { useEffect, useRef, useState } from 'react';
import {
  Check,
  Clock3,
  MessageSquareText,
  ShieldCheck,
  SquareCheckBig,
} from 'lucide-react';
import { LoginForm } from '@/components/fork/LoginForm';
import { useAuth } from '@/lib/fork/auth-context';
import { productName } from '@/lib/fork/web-profile';
import {
  acceptSharedTasks,
  loadSharePreview,
  ShareRequestError,
  type ShareKind,
  type SharePreview,
} from '@/lib/fork/share-client';

type PreviewState =
  | { status: 'loading' }
  | { status: 'ready'; preview: SharePreview }
  | { status: 'unavailable' }
  | { status: 'error' };

const acceptMessage = (error: unknown) => {
  if (!(error instanceof ShareRequestError))
    return 'The tasks could not be saved. Please try again.';
  return {
    invalid: 'This share link is invalid.',
    unavailable: 'These shared tasks are no longer available.',
    temporary: 'The tasks could not be saved. Please try again.',
    'sign-in': 'Your session expired. Please sign in again.',
    self: 'You cannot add tasks shared from your own account.',
    'already-accepted': 'You already added these tasks.',
    locked: 'These tasks are no longer available to add.',
  }[error.code];
};

function readableDate(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

function PrivacyMetadata({ kind }: { kind: ShareKind }) {
  useEffect(() => {
    const prior = document.title;
    const robots = document.createElement('meta');
    robots.name = 'robots';
    robots.content = 'noindex, nofollow, noarchive';
    const referrer = document.createElement('meta');
    referrer.name = 'referrer';
    referrer.content = 'no-referrer';
    document.head.append(robots, referrer);
    document.title = `Shared ${kind === 'chat' ? 'chat' : 'tasks'} · ${productName()}`;
    return () => {
      document.title = prior;
      robots.remove();
      referrer.remove();
    };
  }, [kind]);
  return null;
}

export function ShareExperience({ kind, token }: { kind: ShareKind; token: string }) {
  const { user, loading: authLoading, getToken } = useAuth();
  const [state, setState] = useState<PreviewState>({ status: 'loading' });
  const [showLogin, setShowLogin] = useState(false);
  const [accepting, setAccepting] = useState(false);
  const [accepted, setAccepted] = useState<number | null>(null);
  const [acceptError, setAcceptError] = useState<string | null>(null);
  const acceptanceOwner = useRef(0);
  const name = productName();

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: 'loading' });
    loadSharePreview(kind, token, { signal: controller.signal }).then(
      (preview) => setState({ status: 'ready', preview }),
      (error) => {
        if (controller.signal.aborted) return;
        setState({
          status:
            error instanceof ShareRequestError &&
            ['invalid', 'unavailable'].includes(error.code)
              ? 'unavailable'
              : 'error',
        });
      },
    );
    return () => controller.abort();
  }, [kind, token]);

  useEffect(() => {
    acceptanceOwner.current += 1;
    setAccepting(false);
    setAccepted(null);
    setAcceptError(null);
    if (user) setShowLogin(false);
  }, [token, user?.uid]);

  async function accept() {
    if (!user) {
      setShowLogin(true);
      return;
    }
    const owner = ++acceptanceOwner.current;
    setAccepting(true);
    setAcceptError(null);
    try {
      const bearer = await getToken();
      if (!bearer) throw new ShareRequestError('sign-in');
      const result = await acceptSharedTasks(token, bearer);
      if (owner === acceptanceOwner.current) setAccepted(result.count);
    } catch (error) {
      if (owner === acceptanceOwner.current) setAcceptError(acceptMessage(error));
    } finally {
      if (owner === acceptanceOwner.current) setAccepting(false);
    }
  }

  return (
    <main className="min-h-screen bg-neutral-950 px-5 py-8 text-white sm:px-8 sm:py-14">
      <PrivacyMetadata kind={kind} />
      <div className="mx-auto max-w-2xl">
        <header className="mb-8 flex items-center justify-between border-b border-white/10 pb-5">
          <span className="text-sm font-semibold tracking-wide">{name}</span>
          <span className="flex items-center gap-2 text-xs text-neutral-400">
            <ShieldCheck aria-hidden="true" className="h-4 w-4" />
            Private link
          </span>
        </header>

        {state.status === 'loading' && (
          <section aria-live="polite" className="rounded-2xl border border-white/10 p-8">
            <div className="h-4 w-28 animate-pulse rounded bg-white/10" />
            <div className="mt-5 h-7 w-3/4 animate-pulse rounded bg-white/10" />
            <p className="sr-only">Loading shared content</p>
          </section>
        )}

        {state.status === 'unavailable' && (
          <section role="status" className="rounded-2xl border border-white/10 p-8">
            <p className="text-xs font-medium uppercase tracking-[0.18em] text-neutral-500">
              Link unavailable
            </p>
            <h1 className="mt-3 text-2xl font-semibold">This private link has expired</h1>
            <p className="mt-3 text-sm leading-6 text-neutral-400">
              Ask the sender for a new link. No account details or private content were
              shown.
            </p>
          </section>
        )}

        {state.status === 'error' && (
          <section role="alert" className="rounded-2xl border border-white/10 p-8">
            <p className="text-xs font-medium uppercase tracking-[0.18em] text-neutral-500">
              Preview unavailable
            </p>
            <h1 className="mt-3 text-2xl font-semibold">We could not open this link</h1>
            <p className="mt-3 text-sm leading-6 text-neutral-400">
              Try again in a moment. The link was not accepted or changed.
            </p>
          </section>
        )}

        {state.status === 'ready' && (
          <section>
            <p className="text-xs font-medium uppercase tracking-[0.18em] text-neutral-500">
              Shared by {state.preview.senderName}
            </p>
            <h1 className="mt-3 text-3xl font-semibold tracking-tight sm:text-4xl">
              {kind === 'chat' ? 'A conversation for you' : 'Tasks you can add'}
            </h1>
            <p className="mt-3 text-sm leading-6 text-neutral-400">
              {kind === 'chat'
                ? 'Only the messages selected by the sender appear here.'
                : 'Review the task details before adding a copy to your account.'}
            </p>

            <div className="mt-8 space-y-3">
              {state.preview.kind === 'chat'
                ? state.preview.messages.map((message, index) => (
                    <article
                      key={index}
                      className="rounded-2xl border border-white/10 bg-white/[0.03] p-5"
                    >
                      <div className="flex items-center justify-between gap-4 text-xs text-neutral-500">
                        <span>
                          {message.sender === 'human' ? state.preview.senderName : name}
                        </span>
                        {readableDate(message.createdAt) && (
                          <time dateTime={message.createdAt ?? undefined}>
                            {readableDate(message.createdAt)}
                          </time>
                        )}
                      </div>
                      <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-neutral-200">
                        {message.text}
                      </p>
                    </article>
                  ))
                : state.preview.tasks.map((task, index) => (
                    <article
                      key={index}
                      className="flex gap-4 rounded-2xl border border-white/10 bg-white/[0.03] p-5"
                    >
                      <SquareCheckBig
                        aria-hidden="true"
                        className="mt-0.5 h-5 w-5 shrink-0 text-neutral-400"
                      />
                      <div className="min-w-0">
                        <p className="break-words text-sm leading-6 text-neutral-100">
                          {task.description}
                        </p>
                        {readableDate(task.dueAt) && (
                          <p className="mt-2 flex items-center gap-1.5 text-xs text-neutral-500">
                            <Clock3 aria-hidden="true" className="h-3.5 w-3.5" />
                            Due {readableDate(task.dueAt)}
                          </p>
                        )}
                      </div>
                    </article>
                  ))}
            </div>

            {state.preview.kind === 'tasks' && (
              <div className="mt-8 border-t border-white/10 pt-7">
                {accepted === null ? (
                  <>
                    <button
                      type="button"
                      disabled={accepting || authLoading}
                      onClick={() => void accept()}
                      className="inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-xl bg-white px-5 py-3 text-sm font-semibold text-black transition hover:bg-neutral-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto"
                    >
                      <Check aria-hidden="true" className="h-4 w-4" />
                      {accepting
                        ? 'Adding tasks…'
                        : authLoading
                        ? 'Checking sign in…'
                        : user
                        ? 'Add to my tasks'
                        : 'Sign in to add tasks'}
                    </button>
                    {acceptError && (
                      <p role="alert" className="mt-3 text-sm text-red-300">
                        {acceptError}
                      </p>
                    )}
                  </>
                ) : (
                  <div role="status" className="rounded-xl border border-white/10 p-4">
                    <p className="flex items-center gap-2 text-sm font-medium">
                      <Check aria-hidden="true" className="h-4 w-4" />
                      {accepted} {accepted === 1 ? 'task was' : 'tasks were'} added
                    </p>
                    <a
                      className="mt-2 inline-block text-sm text-neutral-300 underline"
                      href="/tasks"
                    >
                      Open my tasks
                    </a>
                  </div>
                )}
              </div>
            )}

            {showLogin && !user && (
              <div className="mt-8 rounded-2xl border border-white/10 bg-white/[0.02] p-6 sm:p-8">
                <LoginForm onComplete={() => setShowLogin(false)} />
              </div>
            )}
          </section>
        )}

        <footer className="mt-10 flex items-center gap-2 text-xs text-neutral-600">
          <MessageSquareText aria-hidden="true" className="h-4 w-4" />
          Shared content is available only to people with this link.
        </footer>
      </div>
    </main>
  );
}
