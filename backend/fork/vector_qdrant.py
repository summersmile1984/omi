"""Qdrant implementation of the existing vector_db index boundary.

The explicit CLI owns collection creation; serving only admits matching vector
schemas. No Pinecone credentials, provider fallback or successful empty result
is synthesized when Qdrant is unavailable.
"""

from dataclasses import dataclass, field
import math
import os
import re
from urllib.parse import urlsplit
import uuid

import httpx

from .vector_filter import translate
from .model_contract import EmbeddingContract

NAMESPACES = ('ns1', 'ns2', 'ns3', 'ns4', 'ns_tchunks', 'ns_x', 'workstream-association-v1')
_POINT_NAMESPACE = uuid.UUID('b35f5a23-436a-4504-8eb6-274e1f22d3e0')


class VectorStoreUnavailable(RuntimeError):
    """The configured authority could not prove or complete an operation."""


@dataclass(frozen=True)
class Config:
    url: str
    api_key: str = field(repr=False)
    prefix: str
    embedding_contract: EmbeddingContract

    @property
    def dimension(self):
        return self.embedding_contract.dimension

    @classmethod
    def from_env(cls):
        url = os.environ.get('QDRANT_URL', '').rstrip('/')
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ('http', 'https')
            or not parsed.hostname
            or parsed.username
            or parsed.query
            or parsed.fragment
            or parsed.path not in ('', '/')
        ):
            raise ValueError('QDRANT_URL must be an explicit HTTP(S) service origin')
        prefix = os.environ.get('QDRANT_COLLECTION_PREFIX', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', prefix):
            raise ValueError('QDRANT_COLLECTION_PREFIX must contain 1..80 safe identifier characters')
        key = os.environ.get('QDRANT_API_KEY', '')
        if not key:
            raise ValueError('QDRANT_API_KEY is required')
        from .embedding import selected_contract

        model = selected_contract()
        return cls(url, key, prefix, model)


class QdrantIndex:
    def __init__(self, config, *, transport=None):
        self.config = config
        self.client = httpx.Client(
            base_url=config.url,
            headers={'api-key': config.api_key},
            timeout=10,
            follow_redirects=False,
            transport=transport,
        )

    def close(self):
        self.client.close()

    def _collection(self, namespace):
        if namespace not in NAMESPACES:
            raise ValueError('unknown vector namespace; update the migration owner before serving')
        return self.config.prefix + '_' + namespace

    def _request(self, method, path, data=None, *, allow_missing=False):
        try:
            response = self.client.request(method, path, json=data)
        except httpx.HTTPError as error:
            raise VectorStoreUnavailable('Qdrant transport unavailable') from error
        if allow_missing and response.status_code == 404:
            return None
        if response.status_code != 200:
            # Never include server response bodies, vectors, metadata or keys.
            raise VectorStoreUnavailable(f'Qdrant rejected operation (HTTP {response.status_code})')
        try:
            body = response.json()
            if body.get('status') != 'ok' or 'result' not in body:
                raise ValueError('unexpected authority response')
            return body['result']
        except (ValueError, AttributeError) as error:
            raise VectorStoreUnavailable('Qdrant returned an invalid response') from error

    def check(self, *, create=False):
        for namespace in NAMESPACES:
            path = '/collections/' + self._collection(namespace)
            result = self._request('GET', path, allow_missing=True)
            if result is None and create:
                self._request(
                    'PUT',
                    path,
                    {
                        'vectors': {'size': self.config.dimension, 'distance': 'Cosine'},
                        'metadata': {'embedding_contract': self.config.embedding_contract.as_dict()},
                    },
                )
                result = self._request('GET', path)
            if result is None:
                raise VectorStoreUnavailable(
                    'Qdrant collections are not migrated; run python -m fork.vector_qdrant migrate'
                )
            contract = (result.get('config', {}).get('metadata') or {}).get('embedding_contract')
            if contract != self.config.embedding_contract.as_dict():
                raise VectorStoreUnavailable(
                    'Qdrant model identity differs or is unbound; use a reviewed new prefix/backfill'
                )
            vectors = result.get('config', {}).get('params', {}).get('vectors', {})
            if vectors.get('size') != self.config.dimension or vectors.get('distance') != 'Cosine':
                raise VectorStoreUnavailable('Qdrant vector schema differs; migrate to a reviewed collection prefix')
        return self

    def _point(self, identifier):
        if not isinstance(identifier, str) or not identifier:
            raise ValueError('vector identifier must be a nonempty string')
        return str(uuid.uuid5(_POINT_NAMESPACE, identifier))

    def _vector(self, values):
        if not isinstance(values, (list, tuple)) or len(values) != self.config.dimension:
            raise ValueError('vector dimension differs from the configured embedding schema')
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            raise ValueError('vector values must be finite numbers')
        return list(values)

    def _payload(self, identifier, metadata):
        if not isinstance(metadata, dict) or not isinstance(metadata.get('uid'), str) or not metadata['uid']:
            raise ValueError('vector metadata requires an account uid')
        for key, value in metadata.items():
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                raise ValueError('unsupported vector metadata field')
            values = value if isinstance(value, list) else [value]
            if any(
                not isinstance(item, (str, int, float, bool)) or (isinstance(item, float) and not math.isfinite(item))
                for item in values
            ):
                raise ValueError('unsupported vector metadata value')
        return {'_omi_id': identifier, 'metadata': metadata}

    def upsert(self, vectors, namespace):
        points = []
        for item in vectors:
            identifier = item['id']
            points.append(
                {
                    'id': self._point(identifier),
                    'vector': self._vector(item['values']),
                    'payload': self._payload(identifier, item['metadata']),
                }
            )
        path = '/collections/' + self._collection(namespace) + '/points?wait=true'
        if points:
            self._request('PUT', path, {'points': points})
        return {'upserted_count': len(points)}

    def query(self, *, vector, top_k, filter, namespace, include_metadata=False, include_values=False):
        if type(top_k) is not int or not 1 <= top_k <= 10000:
            raise ValueError('vector query limit is out of range')
        path = '/collections/' + self._collection(namespace) + '/points/search'
        points = self._request(
            'POST',
            path,
            {
                'vector': self._vector(vector),
                'limit': top_k,
                'filter': translate(filter),
                'with_payload': True,
                'with_vector': include_values,
            },
        )
        matches = []
        for point in points:
            payload = point['payload']
            match = {'id': payload['_omi_id'], 'score': point['score']}
            if include_metadata:
                match['metadata'] = payload['metadata']
            if include_values:
                match['values'] = point['vector']
            matches.append(match)
        return {'matches': matches}

    def delete(self, *, namespace, ids=None, filter=None):
        if (ids is None) == (filter is None):
            raise ValueError('vector delete requires exactly one explicit selector')
        selector = (
            {'points': [self._point(identifier) for identifier in ids]}
            if ids is not None
            else {'filter': translate(filter)}
        )
        if ids == []:
            return {}
        self._request('POST', '/collections/' + self._collection(namespace) + '/points/delete?wait=true', selector)
        return {}

    def count_owner(self, uid, namespace):
        if not isinstance(uid, str) or not uid:
            raise ValueError('vector owner is required')
        result = self._request(
            'POST',
            '/collections/' + self._collection(namespace) + '/points/count',
            {'filter': translate({'uid': uid}), 'exact': True},
        )
        if type(result.get('count')) is not int or result['count'] < 0:
            raise VectorStoreUnavailable('Qdrant returned an invalid owner count')
        return result['count']

    def purge_owner(self, uid):
        deleted = 0
        for namespace in NAMESPACES:
            deleted += self.count_owner(uid, namespace)
            self.delete(namespace=namespace, filter={'uid': uid})
            if self.count_owner(uid, namespace):
                raise VectorStoreUnavailable('Qdrant owner purge left residual points')
        return deleted

    def update(self, id, *, set_metadata, namespace):
        # Read the exact point once, then atomically merge only requested nested
        # metadata keys; avoid replacing unrelated fields written by another job.
        path = '/collections/' + self._collection(namespace) + '/points'
        points = self._request('POST', path, {'ids': [self._point(id)], 'with_payload': True, 'with_vector': False})
        if not points:
            return {}
        metadata = {**points[0]['payload']['metadata'], **set_metadata}
        self._payload(id, metadata)
        if metadata['uid'] != points[0]['payload']['metadata']['uid']:
            raise ValueError('vector metadata update cannot change account ownership')
        self._request(
            'POST',
            path + '/payload?wait=true',
            {'points': [self._point(id)], 'key': 'metadata', 'payload': set_metadata},
        )
        return {}

    def list(self, *, prefix, namespace):
        if not isinstance(prefix, str) or not prefix:
            raise ValueError('vector listing requires an explicit prefix')
        path = '/collections/' + self._collection(namespace) + '/points/scroll'
        offset = None
        seen = set()
        while True:
            body = {'limit': 256, 'with_payload': ['_omi_id'], 'with_vector': False}
            if offset is not None:
                body['offset'] = offset
            result = self._request('POST', path, body)
            ids = [
                point['payload']['_omi_id']
                for point in result['points']
                if point['payload']['_omi_id'].startswith(prefix)
            ]
            if ids:
                yield ids
            offset = result.get('next_page_offset')
            if offset is None:
                return
            if offset in seen:
                raise VectorStoreUnavailable('Qdrant pagination did not advance')
            seen.add(offset)


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('migrate', 'check'))
    args = parser.parse_args()
    with_index = QdrantIndex(Config.from_env())
    try:
        with_index.check(create=args.command == 'migrate')
        print('Qdrant vector schema is current')
    finally:
        with_index.close()


if __name__ == '__main__':
    main()
