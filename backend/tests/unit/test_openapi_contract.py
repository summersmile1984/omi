import json
import os
import socket

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from scripts import export_openapi


@pytest.fixture(autouse=True)
def narrow_undocumented_route_allowlist(monkeypatch):
    monkeypatch.setattr(
        export_openapi,
        'UNDOCUMENTED_PUBLIC_ROUTES',
        {
            (
                'POST',
                '/v1/conversations/from-segments',
            ): 'Synthetic Firebase-authenticated app-client alias.',
        },
    )


def _make_app() -> FastAPI:
    app = FastAPI()

    @app.get('/v1/dev/example', operation_id='getExample', tags=['Examples'])
    def get_example():
        return {'ok': True}

    @app.post('/v1/internal/example', operation_id='postInternalExample', tags=['Internal'])
    def post_internal_example():
        return {'ok': True}

    @app.post('/v1/conversations/from-segments', operation_id='postFromSegmentsAlias', tags=['Conversations'])
    def post_from_segments_alias():
        return {'ok': True}

    return app


def test_public_openapi_filters_to_developer_contract():
    schema = export_openapi.build_public_openapi(_make_app())

    assert list(schema['paths']) == ['/v1/dev/example']
    assert schema['paths']['/v1/dev/example']['get']['operationId'] == 'getExample'
    assert schema['components']['securitySchemes'] == {
        'developerApiKey': export_openapi.DEVELOPER_API_KEY_AUTH_SCHEME,
        'firebaseBearer': export_openapi.FIREBASE_BEARER_AUTH_SCHEME,
    }
    assert schema['paths']['/v1/dev/example']['get']['security'] == [{'developerApiKey': []}]
    assert schema['paths']['/v1/dev/example']['get']['responses']['401'] == {'$ref': '#/components/responses/Error401'}


def test_route_inventory_fails_when_public_route_missing_from_spec():
    app = _make_app()
    schema = {
        'openapi': '3.1.0',
        'paths': {},
        'components': {'securitySchemes': {'developerApiKey': export_openapi.DEVELOPER_API_KEY_AUTH_SCHEME}},
    }

    with pytest.raises(export_openapi.OpenAPIContractError, match='public routes missing from OpenAPI'):
        export_openapi.assert_route_inventory(app, schema)


def test_cloudflare_route_inventory_includes_http_and_websocket_routes():
    app = _make_app()

    @app.api_route('/v1/health', methods=['GET', 'HEAD'])
    def health():
        return {'ok': True}

    @app.websocket('/v4/example')
    async def example_socket(websocket):
        await websocket.close()

    inventory = export_openapi.build_cloudflare_route_inventory(app)

    assert inventory['source'] == 'backend/main.py'
    assert inventory['version'] == 1
    assert {'method': 'GET', 'path': '/v1/dev/example', 'protocol': 'http'} in inventory['routes']
    assert {'method': 'HEAD', 'path': '/v1/health', 'protocol': 'http'} in inventory['routes']
    assert {'method': 'WEBSOCKET', 'path': '/v4/example', 'protocol': 'websocket'} in inventory['routes']


def test_cloudflare_route_inventory_requires_explicit_new_route_classification(tmp_path):
    inventory_path = tmp_path / 'backend-routes.json'
    generated = {
        'source': 'backend/main.py',
        'version': 1,
        'routes': [{'method': 'GET', 'path': '/v1/example', 'protocol': 'http'}],
    }

    export_openapi.write_cloudflare_route_inventory(inventory_path, generated)
    with pytest.raises(export_openapi.OpenAPIContractError, match='unclassified Cloudflare backend routes'):
        export_openapi.check_cloudflare_route_inventory(inventory_path, generated)

    reviewed = json.loads(inventory_path.read_text())
    reviewed['routes'][0].update({'migration_state': 'legacy-owned', 'owner': 'legacy', 'target_runtime': 'legacy'})
    inventory_path.write_text(export_openapi.stable_cloudflare_route_inventory_json(reviewed))
    export_openapi.check_cloudflare_route_inventory(inventory_path, generated)

    changed = {
        **generated,
        'routes': [
            *generated['routes'],
            {'method': 'POST', 'path': '/v1/new', 'protocol': 'http'},
        ],
    }
    with pytest.raises(export_openapi.OpenAPIContractError, match='unclassified Cloudflare backend routes'):
        export_openapi.check_cloudflare_route_inventory(inventory_path, changed)


def test_cloudflare_route_inventory_preserves_reviewed_ownership_on_regeneration(tmp_path):
    inventory_path = tmp_path / 'backend-routes.json'
    current = {
        'source': 'backend/main.py',
        'version': 1,
        'routes': [
            {
                'method': 'GET',
                'path': '/v1/example',
                'protocol': 'http',
                'migration_state': 'staging-owned',
                'owner': 'api-core',
                'target_runtime': 'python-worker',
            }
        ],
    }
    inventory_path.write_text(export_openapi.stable_cloudflare_route_inventory_json(current))

    export_openapi.write_cloudflare_route_inventory(
        inventory_path,
        {
            'source': 'backend/main.py',
            'version': 1,
            'routes': [
                {'method': 'GET', 'path': '/v1/example', 'protocol': 'http'},
                {'method': 'POST', 'path': '/v1/new', 'protocol': 'http'},
            ],
        },
    )

    regenerated = json.loads(inventory_path.read_text())
    assert regenerated['routes'][0]['migration_state'] == 'staging-owned'
    assert regenerated['routes'][0]['owner'] == 'api-core'
    assert regenerated['routes'][1]['migration_state'] == 'unclassified'


def test_public_like_route_must_be_documented_or_allowlisted():
    app = _make_app()

    @app.get('/v1/conversations/new-public-route', operation_id='newPublicRoute')
    def new_public_route():
        return {'ok': True}

    with pytest.raises(export_openapi.OpenAPIContractError, match='public routes missing from OpenAPI'):
        export_openapi.build_public_openapi(app)


def test_exact_v1_conversations_route_is_audited_when_not_allowlisted(monkeypatch):
    monkeypatch.setattr(export_openapi, 'UNDOCUMENTED_PUBLIC_ROUTES', {})
    app = _make_app()

    @app.post('/v1/conversations', operation_id='createFirstPartyConversation')
    def create_first_party_conversation():
        return {'ok': True}

    with pytest.raises(export_openapi.OpenAPIContractError, match='POST /v1/conversations'):
        export_openapi.build_public_openapi(app)


def test_similar_prefix_without_path_boundary_is_not_audited():
    app = _make_app()

    @app.get('/v1/conversations-v2/preview', operation_id='conversationV2Preview')
    def conversation_v2_preview():
        return {'ok': True}

    schema = export_openapi.build_public_openapi(app)

    assert '/v1/conversations-v2/preview' not in schema['paths']


def test_firebase_key_routes_get_firebase_auth_scheme():
    app = FastAPI()

    @app.get('/v1/dev/keys', operation_id='listApiKeys')
    def list_api_keys():
        return []

    @app.post('/v1/conversations/from-segments', operation_id='postFromSegmentsAlias', tags=['Conversations'])
    def post_from_segments_alias():
        return {'ok': True}

    schema = export_openapi.build_public_openapi(app)

    assert schema['paths']['/v1/dev/keys']['get']['security'] == [{'firebaseBearer': []}]


def test_component_names_follow_explicit_schema_titles():
    class InternalRuntimeName(BaseModel):
        model_config = ConfigDict(title='PublicContractName')

        value: str

    app = FastAPI()

    @app.get('/v1/dev/titled', response_model=InternalRuntimeName, operation_id='getTitled')
    def get_titled():
        return {'value': 'ok'}

    @app.post('/v1/conversations/from-segments', operation_id='postFromSegmentsAlias', tags=['Conversations'])
    def post_from_segments_alias():
        return {'ok': True}

    schema = export_openapi.build_public_openapi(app)

    assert 'PublicContractName' in schema['components']['schemas']
    assert 'InternalRuntimeName' not in schema['components']['schemas']
    assert schema['paths']['/v1/dev/titled']['get']['responses']['200']['content']['application/json']['schema'] == {
        '$ref': '#/components/schemas/PublicContractName'
    }


def test_operation_id_uniqueness_check_reports_duplicates():
    schema = {
        'paths': {
            '/v1/dev/a': {'get': {'operationId': 'duplicateOperation'}},
            '/v1/dev/b': {'post': {'operationId': 'duplicateOperation'}},
        }
    }

    with pytest.raises(export_openapi.OpenAPIContractError, match='duplicate operationId'):
        export_openapi.assert_unique_operation_ids(schema)


def test_check_spec_detects_stale_file(tmp_path):
    spec_path = tmp_path / 'openapi.json'
    spec_path.write_text(json.dumps({'stale': True}) + '\n')

    with pytest.raises(export_openapi.OpenAPIContractError, match='is stale'):
        export_openapi.check_spec(spec_path, json.dumps({'fresh': True}) + '\n')


def test_network_recorder_fails_even_when_blocked_attempt_is_swallowed():
    with export_openapi.record_and_block_outbound_network() as attempts:
        try:
            socket.getaddrinfo('example.com', 443)
        except export_openapi.OpenAPIContractError:
            pass

    assert attempts == ["getaddrinfo: 'example.com'"]


def test_hermetic_environment_restore_pattern():
    before = dict(os.environ)
    export_openapi.configure_hermetic_environment()
    os.environ.clear()
    os.environ.update(before)

    assert dict(os.environ) == before


def test_env_mutation_detector_reports_added_changed_and_removed_keys(monkeypatch):
    monkeypatch.setenv('OPENAPI_TEST_EXISTING', 'before')
    expected = dict(os.environ)
    monkeypatch.setenv('OPENAPI_TEST_EXISTING', 'after')
    monkeypatch.setenv('OPENAPI_TEST_ADDED', 'value')
    monkeypatch.delenv('PATH', raising=False)

    with pytest.raises(export_openapi.OpenAPIContractError, match='mutated environment'):
        export_openapi.assert_env_unchanged(expected)


def test_restorable_side_effect_paths_remove_only_empty_created_dirs(tmp_path, monkeypatch):
    restorable_dir = tmp_path / 'created-empty'
    nonempty_dir = tmp_path / 'created-nonempty'
    monkeypatch.setattr(
        export_openapi,
        'RESTORABLE_SIDE_EFFECT_PATHS',
        {
            restorable_dir: 'test-created empty dir',
            nonempty_dir: 'test-created non-empty dir',
        },
    )
    snapshot = {
        restorable_dir: (False, None, None),
        nonempty_dir: (False, None, None),
    }
    restorable_dir.mkdir()
    nonempty_dir.mkdir()
    (nonempty_dir / 'kept.txt').write_text('keep')

    export_openapi.restore_restorable_side_effect_paths(snapshot)

    assert not restorable_dir.exists()
    assert nonempty_dir.exists()
    with pytest.raises(export_openapi.OpenAPIContractError, match='mutated side-effect paths'):
        export_openapi.assert_no_side_effect_path_mutations(snapshot)


def test_snapshot_side_effect_paths_tracks_file_and_directory_state(tmp_path, monkeypatch):
    tracked_file = tmp_path / 'tracked.json'
    tracked_dir = tmp_path / 'tracked-dir'
    tracked_file.write_text('{}')
    tracked_dir.mkdir()
    monkeypatch.setattr(export_openapi, 'SIDE_EFFECT_PATHS', (tracked_file, tracked_dir, tmp_path / 'missing'))

    snapshot = export_openapi.snapshot_side_effect_paths()

    assert snapshot[tracked_file][0] is True
    assert snapshot[tracked_file][2] == 2
    assert snapshot[tracked_dir][0] is True
    assert snapshot[tracked_dir][2] is None
    assert snapshot[tmp_path / 'missing'] == (False, None, None)
