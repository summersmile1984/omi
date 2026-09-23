"""Bind upstream's index handle to the selected vector authority before routers."""

from ..registry import Patch


def _index(original):
    from ..profile import current

    authority = current().get('data_plane', {}).get('vector')
    if authority == 'pgvector':
        from ..vector_pg import Config, PgVectorIndex

        return PgVectorIndex(Config.from_env()).check()
    if authority == 'qdrant':
        from ..vector_qdrant import Config, QdrantIndex

        return QdrantIndex(Config.from_env()).check()
    raise ValueError('self-host vector authority is not supported')


def patches():
    return [
        Patch(
            name='vector.selected-index',
            module='database.vector_db',
            attribute='index',
            build=_index,
            applies_to=lambda row: row.get('target') == 'self_hosted',
            reason='the selected self-host vector declaration must reach actual vector business operations',
        )
    ]
