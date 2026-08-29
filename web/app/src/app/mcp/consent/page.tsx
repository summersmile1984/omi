'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { ShieldCheck } from 'lucide-react';
import { useAuth } from '@/components/auth/AuthProvider';
import {
  getBetterAuthOAuthClient,
  signedOAuthQuery,
  submitBetterAuthOAuthConsent,
  type BetterAuthOAuthClient,
} from '@/lib/better-auth';

const SCOPE_LABELS: Record<string, string> = {
  'action_items.read': 'Read your action items',
  'action_items.write': 'Create and update your action items',
  'chat.read': 'Read your Omi chat history',
  'conversations.read': 'Read your conversations and daily summaries',
  'goals.read': 'Read your goals',
  'memories.read': 'Read your memories and imported X posts',
  'memories.write': 'Create and update your memories',
  'people.read': 'Read people identified in your conversations',
  'screen_activity.read': 'Read your desktop screen activity',
  offline_access: 'Stay connected until you revoke access',
};

function requestedScopes(query: string): string[] {
  const scope = new URLSearchParams(query).get('scope') || '';
  return [...new Set(scope.split(' ').filter(Boolean))].slice(0, 32);
}

export default function McpConsentPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [oauthQuery, setOAuthQuery] = useState<string>();
  const [client, setClient] = useState<BetterAuthOAuthClient>();
  const [error, setError] = useState<string>();
  const [submitting, setSubmitting] = useState<'accept' | 'deny'>();
  const scopes = useMemo(
    () => (oauthQuery ? requestedScopes(oauthQuery) : []),
    [oauthQuery],
  );

  useEffect(() => {
    const query = signedOAuthQuery(window.location.search);
    setOAuthQuery(query);
    if (!query) setError('This authorization request is invalid or has expired.');
  }, []);

  useEffect(() => {
    if (loading || !oauthQuery) return;
    if (!user) {
      router.replace(`/login${window.location.search}`);
      return;
    }
    const clientId = new URLSearchParams(oauthQuery).get('client_id');
    if (!clientId) {
      setError('This authorization request is missing a client ID.');
      return;
    }
    void getBetterAuthOAuthClient(clientId, oauthQuery)
      .then(setClient)
      .catch((reason) =>
        setError(
          reason instanceof Error ? reason.message : 'Unable to load this MCP client.',
        ),
      );
  }, [loading, oauthQuery, router, user]);

  const submit = async (accept: boolean) => {
    if (!oauthQuery) return;
    setSubmitting(accept ? 'accept' : 'deny');
    setError(undefined);
    try {
      window.location.assign(await submitBetterAuthOAuthConsent(accept, oauthQuery));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to save your choice.');
      setSubmitting(undefined);
    }
  };

  if (loading || (!error && (!oauthQuery || !client || !user))) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-neutral-950 text-white">
        <div className="h-9 w-9 animate-spin rounded-full border-2 border-neutral-700 border-t-white" />
      </main>
    );
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-neutral-950 px-5 py-12 text-white">
      <section className="w-full max-w-lg rounded-3xl border border-neutral-800 bg-neutral-900 p-7 shadow-2xl sm:p-9">
        <div className="mb-7 flex h-12 w-12 items-center justify-center rounded-2xl bg-white text-black">
          <ShieldCheck aria-hidden="true" className="h-6 w-6" />
        </div>
        <p className="mb-2 text-sm font-medium uppercase tracking-[0.18em] text-neutral-400">
          Connect to Omi
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">
          {client?.name || 'MCP client'} wants access
        </h1>
        {client?.uri ? (
          <p className="mt-2 break-all text-sm text-neutral-400">{client.uri}</p>
        ) : null}

        {error ? (
          <div
            role="alert"
            className="mt-7 rounded-2xl border border-red-900/70 bg-red-950/40 p-4 text-sm text-red-200"
          >
            {error}
          </div>
        ) : (
          <>
            <p className="mt-7 text-sm leading-6 text-neutral-300">
              This client will only receive the permissions listed below. It will never
              receive your Omi password.
            </p>
            <ul className="mt-5 space-y-3" aria-label="Requested permissions">
              {scopes.map((scope) => (
                <li
                  key={scope}
                  className="flex gap-3 rounded-2xl bg-neutral-950/70 px-4 py-3 text-sm text-neutral-200"
                >
                  <span
                    aria-hidden="true"
                    className="mt-1 h-2 w-2 shrink-0 rounded-full bg-white"
                  />
                  <span>{SCOPE_LABELS[scope] || scope}</span>
                </li>
              ))}
            </ul>
          </>
        )}

        <div className="mt-8 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
          <button
            type="button"
            disabled={Boolean(submitting) || Boolean(error)}
            onClick={() => void submit(false)}
            className="rounded-xl border border-neutral-700 px-5 py-3 text-sm font-medium text-neutral-200 transition hover:bg-neutral-800 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting === 'deny' ? 'Denying…' : 'Deny'}
          </button>
          <button
            type="button"
            disabled={Boolean(submitting) || Boolean(error)}
            onClick={() => void submit(true)}
            className="rounded-xl bg-white px-5 py-3 text-sm font-semibold text-black transition hover:bg-neutral-200 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting === 'accept' ? 'Connecting…' : 'Allow access'}
          </button>
        </div>
      </section>
    </main>
  );
}
