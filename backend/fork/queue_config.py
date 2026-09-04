"""One mapping owns queue destinations and their separate authentication keys."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from .profile import ProfileError


@dataclass(frozen=True)
class Queue:
    name: str
    handler_env: str
    secret_env: str
    path: str

    def validate(self) -> None:
        try:
            parsed = urlsplit(os.environ.get(self.handler_env, ''))
            parsed.port
        except ValueError as error:
            raise ProfileError(f'{self.handler_env} must be a valid HTTP(S) URL') from error
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ProfileError(f'{self.handler_env} must be a credential-free HTTP(S) URL')
        if parsed.path != self.path or parsed.query or parsed.fragment:
            raise ProfileError(f'{self.handler_env} must target {self.path}')
        if len(os.environ.get(self.secret_env, '')) < 32:
            raise ProfileError(f'{self.secret_env} requires at least 32 characters')


QUEUES = (
    Queue('sync', 'SYNC_TASKS_HANDLER_URL', 'QUEUE_REDIS_SYNC_WORKER_SECRET', '/v2/sync-jobs/run'),
    Queue(
        'audio-merge', 'AUDIO_MERGE_HANDLER_URL', 'QUEUE_REDIS_AUDIO_MERGE_WORKER_SECRET', '/v2/audio-merge-jobs/run'
    ),
    Queue(
        'account-deletion',
        'ACCOUNT_DELETION_HANDLER_URL',
        'QUEUE_REDIS_ACCOUNT_DELETION_WORKER_SECRET',
        '/v1/users/account-deletion-wipes/run',
    ),
    Queue(
        'finalization',
        'LISTEN_FINALIZATION_TASKS_HANDLER_URL',
        'QUEUE_REDIS_FINALIZATION_WORKER_SECRET',
        '/v1/conversation-finalization-jobs/run',
    ),
)
