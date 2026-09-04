"""Bind upstream's index handle to the selected vector authority before routers."""

from ..registry import Patch


def _index(original):
    from ..vector_qdrant import Config, QdrantIndex

    return QdrantIndex(Config.from_env()).check()


def patches():
    return [
        Patch(
            name='vector.qdrant-index',
            module='database.vector_db',
            attribute='index',
            build=_index,
            applies_to=lambda row: row.get('target') == 'self_hosted',
            reason='the self-host Qdrant declaration must reach actual vector business operations',
        )
    ]
