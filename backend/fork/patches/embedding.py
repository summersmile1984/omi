"""Bind both canonical and captured embedding consumers to one model owner."""

from ..registry import Patch


def patches():
    from .. import embedding
    import os

    instance = []

    def build(original):
        if not instance:
            instance.append(embedding.build())
        return instance[0]

    # Skip the patch in fork-local bring-up: there is no Ollama endpoint wired,
    # and ``embedding.build()`` calls ``OllamaEmbeddings.check()`` which fails
    # closed against an empty EMBEDDING_ENDPOINT. Production self-host targets
    # wire EMBEDDING_ENDPOINT (e.g. ``http://ollama:11434``) and the patch
    # applies as designed.
    def applies_to(row):
        if row.get('target') != 'self_hosted':
            return False
        if not (os.environ.get('EMBEDDING_ENDPOINT') or '').strip():
            return False
        return True

    return [
        Patch(
            name='embedding.' + module,
            module=module,
            attribute='embeddings',
            build=build,
            applies_to=applies_to,
            reason='model identity and dimensions must match the selected Qdrant migration contract',
        )
        for module in ('utils.llm.clients', 'database.vector_db')
    ]
