export type ShareKind = 'chat' | 'tasks';

export type ChatSharePreview = {
  kind: 'chat';
  senderName: string;
  messages: Array<{ text: string; sender: string; createdAt: string | null }>;
};

export type TaskSharePreview = {
  kind: 'tasks';
  senderName: string;
  tasks: Array<{ description: string; dueAt: string | null }>;
};

export type SharePreview = ChatSharePreview | TaskSharePreview;

export class ShareRequestError extends Error {
  constructor(
    public readonly code:
      | 'invalid'
      | 'unavailable'
      | 'temporary'
      | 'sign-in'
      | 'self'
      | 'already-accepted'
      | 'locked',
  ) {
    super(code);
  }
}

export type ShareFetch = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

const TOKEN = /^[a-f0-9]{32}$/i;
const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value);
const shortText = (value: unknown, maximum: number): value is string =>
  typeof value === 'string' && value.length <= maximum;

export function validShareToken(token: string): boolean {
  return TOKEN.test(token);
}

async function json(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ShareRequestError('temporary');
  }
}

export function controlledSharePayload(
  kind: ShareKind,
  value: unknown,
): Record<string, unknown> {
  if (!isRecord(value) || !shortText(value.sender_name, 120))
    throw new ShareRequestError('temporary');

  if (kind === 'chat') {
    if (
      !Array.isArray(value.messages) ||
      value.messages.length === 0 ||
      value.messages.length > 100 ||
      !Number.isInteger(value.count) ||
      value.count !== value.messages.length
    )
      throw new ShareRequestError(
        Array.isArray(value.messages) && value.messages.length === 0
          ? 'unavailable'
          : 'temporary',
      );
    const messages = value.messages.map((entry) => {
      if (
        !isRecord(entry) ||
        !shortText(entry.text, 1_000_000) ||
        !shortText(entry.sender, 32) ||
        !(
          entry.created_at === null ||
          entry.created_at === undefined ||
          shortText(entry.created_at, 64)
        )
      )
        throw new ShareRequestError('temporary');
      return {
        text: entry.text,
        sender: entry.sender,
        created_at: typeof entry.created_at === 'string' ? entry.created_at : null,
      };
    });
    return {
      sender_name: value.sender_name,
      messages,
      count: messages.length,
    };
  }

  if (
    !Array.isArray(value.tasks) ||
    value.tasks.length === 0 ||
    value.tasks.length > 20 ||
    !Number.isInteger(value.count) ||
    value.count !== value.tasks.length
  )
    throw new ShareRequestError(
      Array.isArray(value.tasks) && value.tasks.length === 0
        ? 'unavailable'
        : 'temporary',
    );
  const tasks = value.tasks.map((entry) => {
    if (
      !isRecord(entry) ||
      !shortText(entry.description, 4_096) ||
      !(
        entry.due_at === null ||
        entry.due_at === undefined ||
        shortText(entry.due_at, 64)
      )
    )
      throw new ShareRequestError('temporary');
    return {
      description: entry.description,
      due_at: typeof entry.due_at === 'string' ? entry.due_at : null,
    };
  });
  return { sender_name: value.sender_name, tasks, count: tasks.length };
}

function previewPath(kind: ShareKind, token: string): string {
  return kind === 'chat'
    ? `/api/proxy/public/v2/messages/shared/${token}`
    : `/api/proxy/public/v1/action-items/shared/${token}`;
}

export async function loadSharePreview(
  kind: ShareKind,
  token: string,
  options: { fetch?: ShareFetch; signal?: AbortSignal } = {},
): Promise<SharePreview> {
  if (!validShareToken(token)) throw new ShareRequestError('invalid');
  let response: Response;
  try {
    response = await (options.fetch ?? fetch)(previewPath(kind, token), {
      cache: 'no-store',
      headers: { Accept: 'application/json' },
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ShareRequestError('temporary');
  }
  if (response.status === 404 || response.status === 410)
    throw new ShareRequestError('unavailable');
  if (!response.ok) throw new ShareRequestError('temporary');
  const value = controlledSharePayload(kind, await json(response));

  if (kind === 'chat') {
    const messages = (value.messages as Array<Record<string, unknown>>).map((entry) => {
      return {
        text: entry.text as string,
        sender: entry.sender as string,
        createdAt: typeof entry.created_at === 'string' ? entry.created_at : null,
      };
    });
    return { kind, senderName: value.sender_name as string, messages };
  }

  const tasks = (value.tasks as Array<Record<string, unknown>>).map((entry) => {
    return {
      description: entry.description as string,
      dueAt: typeof entry.due_at === 'string' ? entry.due_at : null,
    };
  });
  return { kind, senderName: value.sender_name as string, tasks };
}

export async function acceptSharedTasks(
  token: string,
  bearer: string,
  options: { fetch?: ShareFetch; signal?: AbortSignal } = {},
): Promise<{ count: number }> {
  if (!validShareToken(token)) throw new ShareRequestError('invalid');
  let response: Response;
  try {
    response = await (options.fetch ?? fetch)('/api/proxy/v1/action-items/accept', {
      method: 'POST',
      cache: 'no-store',
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${bearer}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ token }),
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ShareRequestError('temporary');
  }
  const error = {
    400: 'self',
    401: 'sign-in',
    402: 'locked',
    404: 'unavailable',
    409: 'already-accepted',
    410: 'unavailable',
  }[response.status] as ShareRequestError['code'] | undefined;
  if (error) throw new ShareRequestError(error);
  if (!response.ok) throw new ShareRequestError('temporary');
  const value = await json(response);
  if (
    !isRecord(value) ||
    !Array.isArray(value.created) ||
    !value.created.every((item) => shortText(item, 256)) ||
    !Number.isInteger(value.count) ||
    value.count !== value.created.length
  )
    throw new ShareRequestError('temporary');
  return { count: value.count as number };
}
