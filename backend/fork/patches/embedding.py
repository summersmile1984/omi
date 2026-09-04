"""Bind both canonical and captured embedding consumers to one model owner."""

from ..registry import Patch


def patches():
    from .. import embedding

    instance = []

    def build(original):
        if not instance:
            instance.append(embedding.build())
        return instance[0]

    return [
        Patch(
            name='embedding.' + module,
            module=module,
            attribute='embeddings',
            build=build,
            applies_to=lambda row: row.get('target') == 'self_hosted',
            reason='model identity and dimensions must match the selected Qdrant migration contract',
        )
        for module in ('utils.llm.clients', 'database.vector_db')
    ]
