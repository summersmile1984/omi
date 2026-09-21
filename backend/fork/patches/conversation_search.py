"""Resolve the lazy data client before the conversation projection reads it."""

from functools import wraps

from ..registry import Patch


def resolve_client(original):
    @wraps(original)
    def resolve():
        return original()()

    return resolve


def patches():
    return [
        Patch(
            name='conversation-search.resolve-client',
            module='utils.conversations.typesense_index',
            attribute='_resolve_firestore_client',
            build=resolve_client,
            applies_to=lambda row: row.get('target') == 'self_hosted',
            reason='the projection needs the selected client, not its lazy factory',
        )
    ]
