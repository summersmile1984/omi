#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Provider-neutral runtime attestation schema and validation helpers.

This module deliberately contains no network or Docker calls.  The runtime
evidence collector supplies the effective Compose configuration and the
actual environment inspected from the running backend container.  The
attestation records only the sanitized provider route, model and source
identity; it never invents a revision for an operator-owned service.
"""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlsplit

SCHEMA_VERSION = 2
PROVIDER_NAMES = frozenset({'pre_recorded_stt', 'generic_llm', 'embedding', 'realtime'})
CAPABILITY_ROUTE_NAMES = frozenset(
    {'stt_diarization', 'realtime', 'speaker_identity', 'tts', 'image_generation', 'file_chat', 'push'}
)
ATTESTATION_KEYS = frozenset(
    {
        'schema_version',
        'status',
        'workload',
        'source',
        'runtime_config_matches_reviewed',
        'providers',
        'capability_routes',
        'external_service_revision',
        'external_model_revision',
        'external_revision_attested',
    }
)
SOURCE_KEYS = frozenset({'image_id', 'git_commit', 'git_tree', 'runtime_config_sha256'})
IMAGE_ID = re.compile(r'^sha256:[0-9a-f]{64}$')
OBJECT_ID = re.compile(r'^[0-9a-f]{40}$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')

# These authorities are managed-service identities, not operator-owned
# endpoints.  Exact host matching (including subdomains) avoids rejecting a
# harmless name such as ``api.openai.com.example.org`` while still preventing
# the official endpoint from being hidden behind a path or userinfo.
FORBIDDEN_OFFICIAL_HOSTS = frozenset(
    {
        'api.anthropic.com',
        'api.deepgram.com',
        'api.elevenlabs.io',
        'api.groq.com',
        'api.hume.ai',
        'api.omi.me',
        'api.openai.com',
        'api.openrouter.ai',
        'api.perplexity.ai',
        'api.x.ai',
        'firebaseio.com',
        'generativelanguage.googleapis.com',
        'googleapis.com',
        'modulate.ai',
        'pinecone.io',
        'posthog.com',
    }
)


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'provider attestation is missing {name}')
    value = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f'provider attestation {name} contains an invalid control character')
    return value


def _safe_origin(value: Any, name: str, *, schemes: set[str]) -> str:
    value = _required_text(value, name)
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as error:
        raise ValueError(f'provider attestation {name} is not a safe endpoint origin') from error
    if (
        parsed.scheme not in schemes
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {'', '/'}
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in parsed.netloc)
    ):
        raise ValueError(f'provider attestation {name} is not a safe endpoint origin')
    lowered = hostname.lower().rstrip('.')
    if any(lowered == blocked or lowered.endswith('.' + blocked) for blocked in FORBIDDEN_OFFICIAL_HOSTS):
        raise ValueError(f'provider attestation {name} targets a forbidden official endpoint host')
    return f'{parsed.scheme}://{parsed.netloc}'


def _safe_path(value: Any, name: str) -> str:
    value = _required_text(value, name)
    if not value.startswith('/') or '?' in value or '#' in value or any(character.isspace() for character in value):
        raise ValueError(f'provider attestation {name} is not a safe endpoint path')
    return value


def _model(value: Any, name: str) -> str:
    value = _required_text(value, name)
    if any(character in value for character in '\r\n\x00'):
        raise ValueError(f'provider attestation {name} contains an invalid model identity')
    return value


def _source(source: Mapping[str, Any]) -> dict[str, str]:
    if set(source) != SOURCE_KEYS:
        raise ValueError('provider attestation source identity has an incomplete or unexpected shape')
    image_id = _required_text(source.get('image_id'), 'source.image_id')
    git_commit = _required_text(source.get('git_commit'), 'source.git_commit')
    git_tree = _required_text(source.get('git_tree'), 'source.git_tree')
    config_sha = _required_text(source.get('runtime_config_sha256'), 'source.runtime_config_sha256')
    if not IMAGE_ID.fullmatch(image_id):
        raise ValueError('provider attestation source image must be content-addressed')
    if not OBJECT_ID.fullmatch(git_commit) or not OBJECT_ID.fullmatch(git_tree):
        raise ValueError('provider attestation source commit/tree must be full Git object IDs')
    if not SHA256.fullmatch(config_sha):
        raise ValueError('provider attestation source config must be a sha256 digest')
    return {
        'image_id': image_id,
        'git_commit': git_commit,
        'git_tree': git_tree,
        'runtime_config_sha256': config_sha,
    }


def _configuration(configuration: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        'stt_prerecorded_model',
        'mlx_moss_diarize_endpoint',
        'mlx_moss_diarize_model',
        'generic_llm_provider',
        'generic_llm_model',
        'generic_llm_transport',
        'generic_llm_endpoint_origin',
        'embedding_provider',
        'embedding_model',
        'embedding_transport',
        'embedding_dimension',
        'realtime_provider',
        'realtime_model',
        'realtime_transport',
        'realtime_endpoint_origin',
        'realtime_wire_protocol',
    }
    missing = required - set(configuration)
    if missing:
        raise ValueError('provider attestation configuration is missing ' + ', '.join(sorted(missing)))
    if any(str(key).lower().endswith(('_key', '_secret', '_token', '_password')) for key in configuration):
        raise ValueError('provider attestation configuration must not contain credentials')
    result = dict(configuration)
    result['stt_prerecorded_model'] = _required_text(result['stt_prerecorded_model'], 'stt_prerecorded_model')
    result['mlx_moss_diarize_endpoint'] = _required_text(
        result['mlx_moss_diarize_endpoint'], 'mlx_moss_diarize_endpoint'
    )
    result['mlx_moss_diarize_model'] = _model(result['mlx_moss_diarize_model'], 'mlx_moss_diarize_model')
    result['generic_llm_provider'] = _required_text(result['generic_llm_provider'], 'generic_llm_provider')
    result['generic_llm_model'] = _model(result['generic_llm_model'], 'generic_llm_model')
    result['generic_llm_transport'] = _required_text(result['generic_llm_transport'], 'generic_llm_transport')
    result['generic_llm_endpoint_origin'] = _safe_origin(
        result['generic_llm_endpoint_origin'], 'generic_llm_endpoint_origin', schemes={'http', 'https'}
    )
    result['embedding_provider'] = _required_text(result['embedding_provider'], 'embedding_provider')
    result['embedding_model'] = _model(result['embedding_model'], 'embedding_model')
    result['embedding_transport'] = _required_text(result['embedding_transport'], 'embedding_transport')
    dimension = _required_text(result['embedding_dimension'], 'embedding_dimension')
    if not dimension.isdecimal() or int(dimension) <= 0:
        raise ValueError('provider attestation embedding_dimension must be a positive integer')
    result['embedding_dimension'] = dimension
    result['realtime_provider'] = _required_text(result['realtime_provider'], 'realtime_provider')
    result['realtime_model'] = _model(result['realtime_model'], 'realtime_model')
    result['realtime_transport'] = _required_text(result['realtime_transport'], 'realtime_transport')
    result['realtime_endpoint_origin'] = _safe_origin(
        result['realtime_endpoint_origin'], 'realtime_endpoint_origin', schemes={'ws', 'wss'}
    )
    result['realtime_wire_protocol'] = _required_text(result['realtime_wire_protocol'], 'realtime_wire_protocol')
    return result


def _provider_payload(configuration: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    config = _configuration(configuration)
    stt_endpoint = urlsplit(config['mlx_moss_diarize_endpoint'])
    stt_origin = _safe_origin(
        f'{stt_endpoint.scheme}://{stt_endpoint.netloc}', 'pre_recorded_stt.endpoint_origin', schemes={'http', 'https'}
    )
    return {
        'pre_recorded_stt': {
            'provider': config['stt_prerecorded_model'],
            'model': config['mlx_moss_diarize_model'],
            'endpoint_origin': stt_origin,
            'endpoint_path': _safe_path(stt_endpoint.path, 'pre_recorded_stt.endpoint_path'),
            'transport': 'openai_compatible_multipart',
        },
        'generic_llm': {
            'provider': config['generic_llm_provider'],
            'model': config['generic_llm_model'],
            'endpoint_origin': config['generic_llm_endpoint_origin'],
            'transport': config['generic_llm_transport'],
        },
        'embedding': {
            'provider': config['embedding_provider'],
            'model': config['embedding_model'],
            'endpoint_origin': config['generic_llm_endpoint_origin'],
            'transport': config['embedding_transport'],
            'dimension': int(config['embedding_dimension']),
        },
        'realtime': {
            'provider': config['realtime_provider'],
            'model': config['realtime_model'],
            'endpoint_origin': config['realtime_endpoint_origin'],
            'transport': config['realtime_transport'],
            'wire_protocol': config['realtime_wire_protocol'],
        },
    }


def _optional_origin(value: Any, name: str, *, schemes: set[str]) -> str:
    """Normalize an optional route origin without treating empty as a URL."""

    if value == '':
        return ''
    return _safe_origin(value, name, schemes=schemes)


def _capability_payload(configuration: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Describe every self-host capability route and its evidence scope.

    The scope fields are deliberately conservative.  In particular, a
    WebSocket marker proves transport/upstream forwarding only, and the
    mlx-audio MOSS endpoint does not expose a signed service/model revision.
    Keeping those facts in the manifest prevents a passing route probe from
    being misread as model provenance or speaker identity equivalence.
    """

    config = _configuration(configuration)
    stt_endpoint = urlsplit(config['mlx_moss_diarize_endpoint'])
    stt_origin = _safe_origin(
        f'{stt_endpoint.scheme}://{stt_endpoint.netloc}', 'stt_diarization.endpoint_origin', schemes={'http', 'https'}
    )
    tts_provider = _required_text(config.get('tts_provider'), 'tts_provider')
    tts_model = _model(config.get('tts_model'), 'tts_model')
    tts_transport = _required_text(config.get('tts_transport'), 'tts_transport')
    tts_origin = _optional_origin(config.get('tts_endpoint_origin'), 'tts.endpoint_origin', schemes={'http', 'https'})
    push_provider = _required_text(config.get('push_provider'), 'push_provider')
    push_model = _model(config.get('push_model'), 'push_model')
    push_transport = _required_text(config.get('push_transport'), 'push_transport')
    push_origin = _optional_origin(config.get('push_endpoint_origin'), 'push.endpoint_origin', schemes={'https'})
    app_icon_transport = _required_text(config.get('app_icon_transport'), 'image_generation.transport')
    app_icon_origin = _optional_origin(
        config.get('app_icon_endpoint_origin'), 'image_generation.endpoint_origin', schemes={'http', 'https'}
    )
    file_chat_provider = _required_text(config.get('file_chat_provider'), 'file_chat.provider')
    file_chat_model = _model(config.get('file_chat_model'), 'file_chat.model')
    file_chat_transport = _required_text(config.get('file_chat_transport'), 'file_chat.transport')
    speaker_provider = _required_text(config.get('speaker_embedding_provider'), 'speaker_identity.provider')
    speaker_model = _model(config.get('speaker_embedding_model'), 'speaker_identity.model')
    return {
        'stt_diarization': {
            'provider': config['stt_prerecorded_model'],
            'model': config['mlx_moss_diarize_model'],
            'endpoint_origin': stt_origin,
            'endpoint_path': _safe_path(stt_endpoint.path, 'stt_diarization.endpoint_path'),
            'transport': 'openai_compatible_multipart',
            'service_revision_attested': False,
            'model_revision_attested': False,
        },
        'realtime': {
            'provider': config['realtime_provider'],
            'model': config['realtime_model'],
            'endpoint_origin': config['realtime_endpoint_origin'],
            'transport': config['realtime_transport'],
            'wire_protocol': config['realtime_wire_protocol'],
            'roundtrip_scope': 'transport_only',
            'model_provenance_attested': False,
        },
        'speaker_identity': {
            'provider': speaker_provider,
            'model': speaker_model,
            'transport': 'local' if speaker_provider == 'sherpa_onnx' else 'operator_http',
            'endpoint_origin': '',
            'identity_scope': 'embedding_only',
            'enrollment_match_attested': False,
        },
        'tts': {
            'provider': tts_provider,
            'model': tts_model,
            'transport': tts_transport,
            'endpoint_origin': tts_origin,
        },
        'image_generation': {
            'transport': app_icon_transport,
            'endpoint_origin': app_icon_origin,
            'model_provenance_attested': False,
        },
        'file_chat': {
            'provider': file_chat_provider,
            'model': file_chat_model,
            'transport': file_chat_transport,
            'endpoint_origin': '',
        },
        'push': {
            'provider': push_provider,
            'model': push_model,
            'transport': push_transport,
            'endpoint_origin': push_origin,
            'receipt_schema': 'omi.push.receipt.v1',
            'delivery_scope': 'receipt_required_per_device',
        },
    }


def build_provider_attestation(
    *,
    expected_configuration: Mapping[str, Any],
    runtime_configuration: Mapping[str, Any],
    source: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an attestation only after effective config equals runtime config."""

    expected = _configuration(expected_configuration)
    runtime = _configuration(runtime_configuration)
    if expected != runtime:
        raise ValueError('provider attestation runtime configuration does not match reviewed configuration')
    normalized_source = _source(source)
    return {
        'schema_version': SCHEMA_VERSION,
        'status': 'passed',
        'workload': 'backend',
        'source': normalized_source,
        'runtime_config_matches_reviewed': True,
        'providers': _provider_payload(runtime),
        'capability_routes': _capability_payload(runtime),
        # The operator-owned model/realtime/STT services do not expose a
        # signed revision through this gate.  Null is intentional: this must
        # never be populated with the Git revision or a guessed model tag.
        'external_service_revision': None,
        'external_model_revision': None,
        'external_revision_attested': False,
    }


def validate_provider_attestation(
    attestation: Mapping[str, Any],
    *,
    expected_configuration: Mapping[str, Any] | None = None,
    expected_source: Mapping[str, Any] | None = None,
) -> None:
    """Validate an acceptance artifact before it can authorize a cutover."""

    if set(attestation) != ATTESTATION_KEYS:
        raise ValueError('provider attestation has an incomplete or unexpected schema')
    if attestation.get('schema_version') != SCHEMA_VERSION or attestation.get('status') != 'passed':
        raise ValueError('provider attestation has an unsupported schema or status')
    if attestation.get('workload') != 'backend':
        raise ValueError('provider attestation must identify the backend workload')
    source = attestation.get('source')
    if not isinstance(source, Mapping):
        raise ValueError('provider attestation source identity is missing')
    normalized_source = _source(source)
    if expected_source is not None and normalized_source != _source(expected_source):
        raise ValueError('provider attestation source identity does not match the running workload')
    if attestation.get('runtime_config_matches_reviewed') is not True:
        raise ValueError('provider attestation is not bound to the reviewed runtime configuration')
    if attestation.get('external_service_revision') is not None:
        raise ValueError('provider attestation must not invent an external service revision')
    if attestation.get('external_model_revision') is not None:
        raise ValueError('provider attestation must not invent an external model revision')
    if attestation.get('external_revision_attested') is not False:
        raise ValueError('provider attestation has an invalid external revision claim')
    providers = attestation.get('providers')
    if not isinstance(providers, Mapping) or set(providers) != PROVIDER_NAMES:
        raise ValueError('provider attestation is missing a required provider route')
    for name, provider in providers.items():
        if not isinstance(provider, Mapping):
            raise ValueError(f'provider attestation route {name} is not an object')
        if any(str(key).lower().endswith(('_key', '_secret', '_token', '_password')) for key in provider):
            raise ValueError(f'provider attestation route {name} contains credentials')
        _required_text(provider.get('provider'), f'{name}.provider')
        _model(provider.get('model'), f'{name}.model')
        _safe_origin(provider.get('endpoint_origin'), f'{name}.endpoint_origin', schemes={'http', 'https', 'ws', 'wss'})
        _required_text(provider.get('transport'), f'{name}.transport')
        if name == 'pre_recorded_stt':
            if set(provider) != {'provider', 'model', 'endpoint_origin', 'endpoint_path', 'transport'}:
                raise ValueError('provider attestation pre_recorded_stt route has an invalid shape')
            if (
                _safe_path(provider.get('endpoint_path'), 'pre_recorded_stt.endpoint_path')
                != '/v1/audio/transcriptions'
            ):
                raise ValueError('provider attestation pre_recorded_stt route has an unsupported endpoint path')
            if (
                provider.get('provider') != 'mlx_moss_diarize'
                or provider.get('transport') != 'openai_compatible_multipart'
            ):
                raise ValueError('provider attestation pre_recorded_stt route has an unsupported provider')
        elif name == 'embedding':
            if set(provider) != {'provider', 'model', 'endpoint_origin', 'transport', 'dimension'}:
                raise ValueError('provider attestation embedding route has an invalid shape')
            dimension = provider.get('dimension')
            if not isinstance(dimension, int) or dimension <= 0:
                raise ValueError('provider attestation embedding dimension is invalid')
            if provider.get('provider') != 'generic' or provider.get('transport') != 'direct':
                raise ValueError('provider attestation embedding route has an unsupported provider')
        elif name == 'realtime':
            if set(provider) != {'provider', 'model', 'endpoint_origin', 'transport', 'wire_protocol'}:
                raise ValueError('provider attestation realtime route has an invalid shape')
            _required_text(provider.get('wire_protocol'), 'realtime.wire_protocol')
            if (
                provider.get('provider') != 'relay'
                or provider.get('transport') != 'websocket_relay'
                or provider.get('wire_protocol') != 'openai_realtime_v1'
            ):
                raise ValueError('provider attestation realtime route has an unsupported provider')
        elif name == 'generic_llm':
            if set(provider) != {'provider', 'model', 'endpoint_origin', 'transport'}:
                raise ValueError('provider attestation generic_llm route has an invalid shape')
            if provider.get('provider') != 'generic' or provider.get('transport') != 'openai_compatible_http':
                raise ValueError('provider attestation generic_llm route has an unsupported provider')
    capability_routes = attestation.get('capability_routes')
    if not isinstance(capability_routes, Mapping) or set(capability_routes) != CAPABILITY_ROUTE_NAMES:
        raise ValueError('provider attestation is missing a required capability route manifest')
    for name, route in capability_routes.items():
        if not isinstance(route, Mapping):
            raise ValueError(f'provider attestation capability route {name} is not an object')
        if any(str(key).lower().endswith(('_key', '_secret', '_token', '_password')) for key in route):
            raise ValueError(f'provider attestation capability route {name} contains credentials')
        if name in {'stt_diarization', 'speaker_identity', 'tts', 'file_chat', 'push'}:
            _required_text(route.get('provider'), f'{name}.provider')
            _model(route.get('model'), f'{name}.model')
        _required_text(route.get('transport'), f'{name}.transport')
        endpoint_origin = route.get('endpoint_origin')
        if endpoint_origin != '':
            _safe_origin(endpoint_origin, f'{name}.endpoint_origin', schemes={'http', 'https', 'ws', 'wss'})
        if name == 'stt_diarization':
            if set(route) != {
                'provider',
                'model',
                'endpoint_origin',
                'endpoint_path',
                'transport',
                'service_revision_attested',
                'model_revision_attested',
            }:
                raise ValueError('provider attestation MOSS route has an invalid shape')
            if route.get('endpoint_path') != '/v1/audio/transcriptions':
                raise ValueError('provider attestation MOSS route has an unsupported endpoint path')
            if route.get('service_revision_attested') is not False or route.get('model_revision_attested') is not False:
                raise ValueError('provider attestation MOSS route must not claim unavailable revision evidence')
        elif name == 'realtime':
            if set(route) != {
                'provider',
                'model',
                'endpoint_origin',
                'transport',
                'wire_protocol',
                'roundtrip_scope',
                'model_provenance_attested',
            }:
                raise ValueError('provider attestation realtime route has an invalid shape')
            if route.get('roundtrip_scope') != 'transport_only' or route.get('model_provenance_attested') is not False:
                raise ValueError('provider attestation realtime route has an overstated provenance claim')
        elif name == 'speaker_identity':
            if set(route) != {
                'provider',
                'model',
                'transport',
                'endpoint_origin',
                'identity_scope',
                'enrollment_match_attested',
            }:
                raise ValueError('provider attestation speaker route has an invalid shape')
            if route.get('identity_scope') != 'embedding_only' or route.get('enrollment_match_attested') is not False:
                raise ValueError('provider attestation speaker route has an overstated identity claim')
        elif name == 'tts':
            if set(route) != {'provider', 'model', 'transport', 'endpoint_origin'}:
                raise ValueError('provider attestation TTS route has an invalid shape')
        elif name == 'image_generation':
            if set(route) != {'transport', 'endpoint_origin', 'model_provenance_attested'}:
                raise ValueError('provider attestation image route has an invalid shape')
            if route.get('model_provenance_attested') is not False:
                raise ValueError('provider attestation image route must not claim model provenance')
        elif name == 'file_chat':
            if set(route) != {'provider', 'model', 'transport', 'endpoint_origin'}:
                raise ValueError('provider attestation file-chat route has an invalid shape')
        elif name == 'push':
            if set(route) != {
                'provider',
                'model',
                'transport',
                'endpoint_origin',
                'receipt_schema',
                'delivery_scope',
            }:
                raise ValueError('provider attestation push route has an invalid shape')
            if (
                route.get('receipt_schema') != 'omi.push.receipt.v1'
                or route.get('delivery_scope') != 'receipt_required_per_device'
            ):
                raise ValueError('provider attestation push route is not bound to the receipt contract')
    if expected_configuration is not None:
        expected = _configuration(expected_configuration)
        expected_routes = _provider_payload(expected)
        if dict(providers) != expected_routes:
            raise ValueError('provider attestation routes do not match effective provider configuration')
        if dict(capability_routes) != _capability_payload(expected):
            raise ValueError('provider attestation capability manifest does not match effective configuration')


def validate_realtime_probe_identity(probe: Mapping[str, Any], configuration: Mapping[str, Any]) -> None:
    """Bind a public realtime probe result to the reviewed runtime route."""

    if not isinstance(probe, Mapping) or probe.get('status') != 'passed':
        raise ValueError('realtime probe did not pass')
    expected = {
        'provider': _required_text(configuration.get('realtime_provider'), 'realtime_provider'),
        'model': _model(configuration.get('realtime_model'), 'realtime_model'),
        'endpoint_origin': _safe_origin(
            configuration.get('realtime_endpoint_origin'), 'realtime_endpoint_origin', schemes={'ws', 'wss'}
        ),
        'transport': _required_text(configuration.get('realtime_transport'), 'realtime_transport'),
        'wire_protocol': _required_text(configuration.get('realtime_wire_protocol'), 'realtime_wire_protocol'),
        'roundtrip_scope': 'transport_only',
        'model_provenance_attested': False,
    }
    observed_keys = set(expected)
    if any(key not in probe for key in observed_keys):
        raise ValueError('realtime probe omitted provider route identity')
    observed = {key: probe[key] for key in observed_keys}
    if observed != expected:
        raise ValueError('realtime probe route identity does not match reviewed configuration')


def validate_tts_probe_identity(probe: Mapping[str, Any], configuration: Mapping[str, Any]) -> None:
    """Bind a public TTS probe result to the reviewed runtime route."""

    if not isinstance(probe, Mapping) or probe.get('status') != 'passed':
        raise ValueError('tts probe did not pass')
    provider = _required_text(configuration.get('tts_provider'), 'tts_provider')
    model = _model(configuration.get('tts_model'), 'tts_model')
    transport = _required_text(configuration.get('tts_transport'), 'tts_transport')
    endpoint_origin = configuration.get('tts_endpoint_origin')
    if endpoint_origin == '':
        expected_endpoint_origin = ''
    else:
        expected_endpoint_origin = _safe_origin(endpoint_origin, 'tts_endpoint_origin', schemes={'http', 'https'})
    expected = {
        'provider': provider,
        'model': model,
        'transport': transport,
        'endpoint_origin': expected_endpoint_origin,
    }
    observed_keys = set(expected)
    if any(key not in probe for key in observed_keys):
        raise ValueError('tts probe omitted provider route identity')
    observed = {key: probe[key] for key in observed_keys}
    if observed != expected:
        raise ValueError('tts probe route identity does not match reviewed configuration')
