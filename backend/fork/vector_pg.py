"""Explicitly migrated, model-bound pgvector authority for self_hosted.local."""

from dataclasses import dataclass
import json
import math
import os
import re

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from fork.firestore_pg.engine import get_engine
from .embedding import selected_contract
from .operator_ai import HostedOperatorAI, select as select_operator_ai
from .profile import current
from .vector_qdrant import NAMESPACES, VectorStoreUnavailable, _HostedEmbeddingIdentity

_SAFE_PREFIX = re.compile(r'[a-z][a-z0-9_]{0,31}\Z')
_FIELD = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


@dataclass(frozen=True)
class Config:
    dsn: str
    prefix: str
    embedding_contract: object

    @property
    def dimension(self):
        return self.embedding_contract.dimension

    @classmethod
    def from_env(cls):
        dsn = os.environ.get('FIRESTORE_PG_DSN', '')
        try:
            url = make_url(dsn)
        except (ArgumentError, ValueError, TypeError) as error:
            raise ValueError('FIRESTORE_PG_DSN must be a PostgreSQL psycopg SQLAlchemy URL') from error
        if url.drivername != 'postgresql+psycopg' or not url.database:
            raise ValueError('FIRESTORE_PG_DSN must be a PostgreSQL psycopg SQLAlchemy URL')
        prefix = os.environ.get('PGVECTOR_COLLECTION_PREFIX', '')
        if not _SAFE_PREFIX.fullmatch(prefix):
            raise ValueError('PGVECTOR_COLLECTION_PREFIX must be a safe lowercase SQL identifier (1..32 characters)')
        row = current()
        if (
            row.get('target') != 'self_hosted'
            or row.get('stage') != 'local'
            or row.get('data_plane', {}).get('vector') != 'pgvector'
        ):
            raise ValueError('pgvector requires the selected self_hosted.local profile')
        selected = select_operator_ai(row)
        if isinstance(selected, HostedOperatorAI):
            if row.get('capabilities', {}).get('embedding_dims') != selected.embedding_dimension:
                raise ValueError('profile embedding capability differs from its hosted model contract')
            model = _HostedEmbeddingIdentity(
                selected.provider, selected.embedding_model, selected.embedding_base_url, selected.embedding_dimension
            )
        else:
            model = selected_contract()
        return cls(dsn, prefix, model)


class _Filter:
    """Bind every value, including JSON keys; never interpolate caller metadata."""

    def __init__(self):
        self.params = {}

    def bind(self, value):
        key = f'p{len(self.params)}'
        self.params[key] = value
        return ':' + key

    def compile(self, filters):
        if not isinstance(filters, dict) or not filters:
            raise ValueError('vector operation requires a nonempty metadata filter')
        parts = []
        for name, expressions in filters.items():
            if name in ('$and', '$or'):
                if not isinstance(expressions, list) or not expressions:
                    raise ValueError('vector logical filter requires operands')
                joiner = ' AND ' if name == '$and' else ' OR '
                parts.append('(' + joiner.join(self.compile(item) for item in expressions) + ')')
                continue
            if not isinstance(name, str) or not _FIELD.fullmatch(name):
                raise ValueError('unsupported vector metadata field')
            key = self.bind(name)
            field = f'(metadata -> {key})'
            expressions = expressions if isinstance(expressions, dict) else {'$eq': expressions}
            if not expressions:
                raise ValueError('empty vector field filter')
            for operator, value in expressions.items():
                if operator == '$exists':
                    if type(value) is not bool:
                        raise ValueError('vector exists filter requires a boolean')
                    parts.append(f'(metadata ? {key})' if value else f'NOT (metadata ? {key})')
                elif operator in ('$eq', '$in'):
                    candidates = value if operator == '$in' else [value]
                    if not isinstance(candidates, list):
                        raise ValueError('vector membership filter requires an array')
                    terms = []
                    for candidate in candidates:
                        _scalar(candidate)
                        scalar = self.bind(json.dumps(candidate, allow_nan=False))
                        array = self.bind(json.dumps([candidate], allow_nan=False))
                        terms.append(
                            f'({field} = CAST({scalar} AS jsonb) OR (jsonb_typeof({field}) = \'array\' AND {field} @> CAST({array} AS jsonb)))'
                        )
                    parts.append('(' + (' OR '.join(terms) if terms else 'FALSE') + ')')
                elif operator in ('$gt', '$gte', '$lt', '$lte'):
                    if type(value) not in (int, float) or not math.isfinite(value):
                        raise ValueError('vector range filter requires a finite number')
                    symbol = {'$gt': '>', '$gte': '>=', '$lt': '<', '$lte': '<='}[operator]
                    # Numeric array fields follow Qdrant's any-element range semantics.
                    bound = self.bind(value)
                    numeric = f"CASE WHEN jsonb_typeof({field}) = 'number' THEN ({field} #>> '{{}}')::numeric {symbol} {bound} ELSE FALSE END"
                    array = f'''EXISTS (SELECT 1 FROM jsonb_array_elements(
                        CASE WHEN jsonb_typeof({field}) = 'array' THEN {field} ELSE '[]'::jsonb END
                    ) AS item(value) WHERE CASE WHEN jsonb_typeof(item.value) = 'number'
                        THEN (item.value #>> '{{}}')::numeric {symbol} {bound} ELSE FALSE END)'''
                    parts.append(f'(({numeric}) OR ({array}))')
                else:
                    raise ValueError('unsupported vector filter operator')
        return '(' + ' AND '.join(parts) + ')'


def _scalar(value):
    if not (isinstance(value, (str, bool)) or type(value) is int or (type(value) is float and math.isfinite(value))):
        raise ValueError('unsupported vector filter value')


def _owner_filter(filters):
    """Prove every OR branch is scoped to one account, including nested clauses."""
    if not isinstance(filters, dict) or not filters:
        raise ValueError('vector operation requires an account-scoped metadata filter')
    owners = []
    for key, value in filters.items():
        if key == 'uid':
            uid = value.get('$eq') if isinstance(value, dict) and set(value) == {'$eq'} else value
            if not isinstance(uid, str) or not uid:
                raise ValueError('vector filter requires exact account uid')
            owners.append(uid)
        elif key == '$and':
            if not isinstance(value, list) or not value:
                raise ValueError('vector logical filter requires operands')
            owners.extend(owner for part in value if (owner := _owner_filter(part)) is not None)
        elif key == '$or':
            if not isinstance(value, list) or not value:
                raise ValueError('vector logical filter requires operands')
            branches = [_owner_filter(part) for part in value]
            if all(branches) and len(set(branches)) == 1:
                owners.append(branches[0])
    if len(set(owners)) > 1:
        raise ValueError('vector filter cannot span account owners')
    return owners[0] if owners else None


class PgVectorIndex:
    def __init__(self, config, *, engine=None):
        if not _SAFE_PREFIX.fullmatch(config.prefix):
            raise ValueError('unsafe PG vector table prefix')
        if type(config.dimension) is not int or not 1 <= config.dimension <= 16000:
            raise ValueError('unsupported vector dimension')
        self.config = config
        self._engine = engine
        self._table = f'public.{config.prefix}_vectors'
        self._binding = f'public.{config.prefix}_identity'

    @property
    def engine(self):
        return self._engine if self._engine is not None else get_engine()

    def close(self):
        # The shared Firestore engine is owned by firestore_pg; never dispose it here.
        pass

    def _execute(self, operation):
        try:
            return operation()
        except SQLAlchemyError as error:
            raise VectorStoreUnavailable('pgvector database unavailable or operation rejected') from error

    def _schema(self, conn):
        tables = conn.execute(
            text('SELECT to_regclass(:vectors), to_regclass(:identity)'),
            {'vectors': self._table, 'identity': self._binding},
        ).one()
        return tuple(item is not None for item in tables)

    def check(self, *, create=False):
        if create:
            self._execute(self._migrate)
        self._execute(self._check)
        return self

    def _migrate(self):
        with self.engine.begin() as conn:
            conn.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'), {'key': self._table})
            state = self._schema(conn)
            if state == (True, True):
                return
            if state != (False, False):
                raise VectorStoreUnavailable('pgvector schema is partial or unbound; refusing to relabel existing data')
            conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public'))
            extension = conn.execute(
                text(
                    "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'"
                )
            ).scalar_one()
            if extension != 'public':
                raise VectorStoreUnavailable('pgvector extension must be installed in public schema')
            dimension = self.config.dimension
            namespaces = ', '.join("'" + namespace.replace("'", "''") + "'" for namespace in NAMESPACES)
            conn.execute(text(f'''CREATE TABLE {self._table} (
                namespace text NOT NULL CHECK (namespace IN ({namespaces})),
                id text NOT NULL, uid text NOT NULL CHECK (uid <> ''),
                embedding public.vector({dimension}) NOT NULL,
                metadata jsonb NOT NULL CHECK (jsonb_typeof(metadata) = 'object' AND metadata ->> 'uid' = uid),
                PRIMARY KEY (namespace, id))'''))
            conn.execute(text(f'CREATE INDEX {self.config.prefix}_vectors_owner ON {self._table} (uid, namespace)'))
            conn.execute(text(f'''CREATE TABLE {self._binding} (
                singleton boolean PRIMARY KEY CHECK (singleton),
                embedding_contract jsonb NOT NULL, dimension integer NOT NULL,
                namespaces jsonb NOT NULL, schema_version integer NOT NULL)'''))
            conn.execute(
                text(
                    f'INSERT INTO {self._binding} VALUES (true, CAST(:contract AS jsonb), :dimension, CAST(:namespaces AS jsonb), 1)'
                ),
                {
                    'contract': json.dumps(self.config.embedding_contract.as_dict(), sort_keys=True),
                    'dimension': dimension,
                    'namespaces': json.dumps(NAMESPACES),
                },
            )

    def _check(self):
        with self.engine.connect() as conn:
            if self._schema(conn) != (True, True):
                raise VectorStoreUnavailable('pgvector is not migrated; run python -m fork.vector_pg migrate')
            rows = conn.execute(
                text(f'SELECT embedding_contract, dimension, namespaces, schema_version FROM {self._binding}')
            ).all()
            if (
                len(rows) != 1
                or rows[0][0] != self.config.embedding_contract.as_dict()
                or rows[0][1] != self.config.dimension
            ):
                raise VectorStoreUnavailable(
                    'pgvector model identity or dimension differs; use a reviewed new prefix/backfill'
                )
            if rows[0][2] != list(NAMESPACES) or rows[0][3] != 1:
                raise VectorStoreUnavailable('pgvector namespace binding differs')
            columns = conn.execute(
                text('''SELECT attname, format_type(atttypid, atttypmod), attnotnull
                FROM pg_attribute WHERE attrelid=to_regclass(:table) AND attnum > 0 AND NOT attisdropped'''),
                {'table': self._table},
            ).all()
            expected = {
                'namespace': 'text',
                'id': 'text',
                'uid': 'text',
                'embedding': f'vector({self.config.dimension})',
                'metadata': 'jsonb',
            }
            if {name: typ for name, typ, _ in columns} != expected or not all(required for _, _, required in columns):
                raise VectorStoreUnavailable('pgvector vector schema differs from the bound dimension')
            constraints = conn.execute(
                text('''SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conrelid=to_regclass(:table)'''),
                {'table': self._table},
            ).all()
            checks = {name: definition for name, kind, definition in constraints if kind == 'c'}
            namespace_check = checks.get(self.config.prefix + '_vectors_namespace_check', '')
            allowed = re.findall(r"'([^']+)'::text", namespace_check)
            if (
                not any(kind == 'p' and 'namespace, id' in definition for _, kind, definition in constraints)
                or sorted(allowed) != sorted(NAMESPACES)
                or 'uid' not in checks.get(self.config.prefix + '_vectors_uid_check', '')
                or 'metadata' not in checks.get(self.config.prefix + '_vectors_check', '')
                or 'uid' not in checks.get(self.config.prefix + '_vectors_check', '')
            ):
                raise VectorStoreUnavailable('pgvector ownership or namespace constraints are missing')
            indexes = (
                conn.execute(
                    text('''SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i
                WHERE i.indrelid=to_regclass(:table) AND i.indisvalid AND i.indisready'''),
                    {'table': self._table},
                )
                .scalars()
                .all()
            )
            if not any('(uid, namespace)' in index for index in indexes):
                raise VectorStoreUnavailable('pgvector owner index differs from the migrated schema')

    def _namespace(self, namespace):
        if namespace not in NAMESPACES:
            raise ValueError('unknown vector namespace; update the migration owner before serving')
        return namespace

    def _vector(self, values):
        if not isinstance(values, (list, tuple)) or len(values) != self.config.dimension:
            raise ValueError('vector dimension differs from the configured embedding schema')
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            raise ValueError('vector values must be finite numbers')
        if not any(value != 0 for value in values):
            raise ValueError('cosine vector must be nonzero')
        return '[' + ','.join(str(value) for value in values) + ']'

    def _metadata(self, metadata):
        if not isinstance(metadata, dict) or not isinstance(metadata.get('uid'), str) or not metadata['uid']:
            raise ValueError('vector metadata requires an account uid')
        for key, value in metadata.items():
            if not isinstance(key, str) or not _FIELD.fullmatch(key):
                raise ValueError('unsupported vector metadata field')
            for item in value if isinstance(value, list) else (value,):
                _scalar(item)
        return json.dumps(metadata, allow_nan=False)

    def upsert(self, vectors, namespace):
        namespace = self._namespace(namespace)
        items = []
        seen = set()
        for item in vectors:
            identifier = item['id']
            if not isinstance(identifier, str) or not identifier or identifier in seen:
                raise ValueError('vector ids must be distinct nonempty strings')
            seen.add(identifier)
            metadata = item['metadata']
            items.append(
                {
                    'namespace': namespace,
                    'id': identifier,
                    'uid': metadata.get('uid'),
                    'vector': self._vector(item['values']),
                    'metadata': self._metadata(metadata),
                }
            )
        if not items:
            return {'upserted_count': 0}

        def run():
            with self.engine.begin() as conn:
                statement = text(f'''INSERT INTO {self._table} (namespace, id, uid, embedding, metadata)
                    VALUES (:namespace, :id, :uid, CAST(:vector AS public.vector), CAST(:metadata AS jsonb))
                    ON CONFLICT (namespace, id) DO UPDATE SET embedding=EXCLUDED.embedding, metadata=EXCLUDED.metadata
                    WHERE {self._table}.uid=EXCLUDED.uid''')
                for item in items:
                    if conn.execute(statement, item).rowcount != 1:
                        raise ValueError('vector upsert cannot change account ownership')

        self._execute(run)
        return {'upserted_count': len(items)}

    def query(self, *, vector, top_k, filter, namespace, include_metadata=False, include_values=False):
        namespace = self._namespace(namespace)
        if type(top_k) is not int or not 1 <= top_k <= 10000:
            raise ValueError('vector query limit is out of range')
        owner = _owner_filter(filter)
        if not owner:
            raise ValueError('vector query requires exact account uid')
        predicate = _Filter()
        condition = predicate.compile(filter)
        params = {
            **predicate.params,
            'namespace': namespace,
            'uid': owner,
            'vector': self._vector(vector),
            'limit': top_k,
        }
        columns = ', metadata' if include_metadata else ''
        columns += ', embedding::text AS values' if include_values else ''

        def run():
            with self.engine.connect() as conn:
                rows = (
                    conn.execute(
                        text(f'''WITH candidates AS MATERIALIZED (
                    SELECT id, embedding, metadata FROM {self._table}
                    WHERE uid=:uid AND namespace=:namespace AND {condition}
                ) SELECT id, 1 - (embedding <=> CAST(:vector AS public.vector)) AS score{columns}
                    FROM candidates ORDER BY embedding <=> CAST(:vector AS public.vector), id LIMIT :limit'''),
                        params,
                    )
                    .mappings()
                    .all()
                )
            matches = []
            for row in rows:
                match = {'id': row['id'], 'score': float(row['score'])}
                if include_metadata:
                    match['metadata'] = row['metadata']
                if include_values:
                    match['values'] = json.loads(row['values'])
                matches.append(match)
            return {'matches': matches}

        return self._execute(run)

    def delete(self, *, namespace, ids=None, filter=None):
        namespace = self._namespace(namespace)
        if (ids is None) == (filter is None):
            raise ValueError('vector delete requires exactly one explicit selector')
        if ids is not None:
            if not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids):
                raise ValueError('vector delete requires valid ids')
            if not ids:
                return {}

            def run():
                with self.engine.begin() as conn:
                    # Bound each ID, not a client-controlled SQL array literal.
                    for offset in range(0, len(ids), 1000):
                        chunk = ids[offset : offset + 1000]
                        binds = {f'id{i}': identifier for i, identifier in enumerate(chunk)}
                        placeholders = ', '.join(':' + key for key in binds)
                        conn.execute(
                            text(f'DELETE FROM {self._table} WHERE namespace=:namespace AND id IN ({placeholders})'),
                            {'namespace': namespace, **binds},
                        )

            self._execute(run)
        else:
            owner = _owner_filter(filter)
            if not owner:
                raise ValueError('vector filter delete requires exact account uid')
            predicate = _Filter()
            condition = predicate.compile(filter)

            def run():
                with self.engine.begin() as conn:
                    conn.execute(
                        text(f'DELETE FROM {self._table} WHERE uid=:uid AND namespace=:namespace AND {condition}'),
                        {'uid': owner, 'namespace': namespace, **predicate.params},
                    )

            self._execute(run)
        return {}

    def count_owner(self, uid, namespace):
        namespace = self._namespace(namespace)
        if not isinstance(uid, str) or not uid:
            raise ValueError('vector owner is required')

        def run():
            with self.engine.connect() as conn:
                return conn.execute(
                    text(f'SELECT count(*) FROM {self._table} WHERE namespace=:namespace AND uid=:uid'),
                    {'namespace': namespace, 'uid': uid},
                ).scalar_one()

        return self._execute(run)

    def purge_owner(self, uid):
        if not isinstance(uid, str) or not uid:
            raise ValueError('vector owner is required')

        def run():
            with self.engine.begin() as conn:
                count = conn.execute(
                    text(f'SELECT count(*) FROM {self._table} WHERE uid=:uid'), {'uid': uid}
                ).scalar_one()
                conn.execute(text(f'DELETE FROM {self._table} WHERE uid=:uid'), {'uid': uid})
                if conn.execute(text(f'SELECT count(*) FROM {self._table} WHERE uid=:uid'), {'uid': uid}).scalar_one():
                    raise VectorStoreUnavailable('pgvector owner purge left residual points')
                return count

        return self._execute(run)

    def update(self, id, *, set_metadata, namespace):
        namespace = self._namespace(namespace)
        if not isinstance(id, str) or not id:
            raise ValueError('vector identifier must be a nonempty string')
        if not isinstance(set_metadata, dict):
            raise ValueError('vector metadata update must be an object')
        # Validate all values before touching the database, even for an absent ID.
        for key, value in set_metadata.items():
            if key == 'uid':
                if not isinstance(value, str) or not value:
                    raise ValueError('vector metadata update cannot change account ownership')
            elif not isinstance(key, str) or not _FIELD.fullmatch(key):
                raise ValueError('unsupported vector metadata field')
            for item in value if isinstance(value, list) else (value,):
                _scalar(item)
        if not set_metadata:
            return {}
        payload = json.dumps(set_metadata, allow_nan=False)

        def run():
            with self.engine.begin() as conn:
                result = conn.execute(
                    text(f'''UPDATE {self._table} SET metadata=metadata || CAST(:metadata AS jsonb)
                    WHERE namespace=:namespace AND id=:id AND (CAST(:uid AS text) IS NULL OR uid=:uid)'''),
                    {'namespace': namespace, 'id': id, 'metadata': payload, 'uid': set_metadata.get('uid')},
                )
                if result.rowcount != 1 and 'uid' in set_metadata:
                    exists = conn.execute(
                        text(f'SELECT 1 FROM {self._table} WHERE namespace=:namespace AND id=:id'),
                        {'namespace': namespace, 'id': id},
                    ).first()
                    if exists:
                        raise ValueError('vector metadata update cannot change account ownership')

        self._execute(run)
        return {}

    def list(self, *, prefix, namespace):
        namespace = self._namespace(namespace)
        if not isinstance(prefix, str) or not prefix:
            raise ValueError('vector listing requires an explicit prefix')
        cursor = None
        while True:

            def run():
                with self.engine.connect() as conn:
                    return (
                        conn.execute(
                            text(f'''SELECT id FROM {self._table}
                        WHERE namespace=:namespace AND left(id, length(:prefix))=:prefix
                        AND (CAST(:cursor AS text) IS NULL OR id > :cursor) ORDER BY id LIMIT 256'''),
                            {'namespace': namespace, 'prefix': prefix, 'cursor': cursor},
                        )
                        .scalars()
                        .all()
                    )

            ids = self._execute(run)
            if not ids:
                return
            yield ids
            cursor = ids[-1]
            if len(ids) < 256:
                return


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('migrate', 'check'))
    args = parser.parse_args()
    index = PgVectorIndex(Config.from_env())
    try:
        index.check(create=args.command == 'migrate')
        print('pgvector schema is current')
    finally:
        index.close()


if __name__ == '__main__':
    main()
