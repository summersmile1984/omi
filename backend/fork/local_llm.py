"""Selected text-model identity and admission; shared by every product feature."""

import math
import os
import time
from urllib.parse import urlsplit

import httpx

from .egress_policy import assert_http_endpoint_allowed
from .model_contract import validate_llm
from .profile import current


class LLMUnavailable(RuntimeError):
    """A selected model did not establish identity or complete its response."""

    status_code = 503


class LLMInputRejected(ValueError):
    """Input exceeds the selected text model's admitted capabilities."""

    status_code = 422


def contract_for_profile():
    row = current()
    if row.get('target') != 'self_hosted':
        raise ValueError('local LLM requires self_hosted')
    contract = validate_llm(row.get('llm'))
    expected = contract.provider if contract else 'disabled'
    if row.get('capabilities', {}).get('llm_provider', 'disabled') != expected:
        raise ValueError('LLM capability conflicts with selected model')
    return contract


class Authority:
    def __init__(self, contract, endpoint, *, transport=None, timeout=120):
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in ('http', 'https')
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ('', '/')
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError('LLM_ENDPOINT must be an explicit service origin')
        assert_http_endpoint_allowed(endpoint)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError('LLM timeout must be within 300 seconds')
        self.contract, self.endpoint, self.transport, self.timeout = contract, endpoint.rstrip('/'), transport, timeout

    def verify(self, version, tags, info):
        contract = self.contract
        models = tags.get('models')
        if not isinstance(models, list):
            raise LLMUnavailable('local model inventory is malformed')
        models = [item for item in models if isinstance(item, dict) and item.get('name') == contract.model]
        metadata = info.get('model_info')
        if not isinstance(metadata, dict) or not isinstance(info.get('modelfile'), str):
            raise LLMUnavailable('local model description is malformed')
        contexts = [value for key, value in metadata.items() if key.endswith('.context_length')]
        sources = [
            line[5:].strip().rsplit('/', 1)[-1] for line in info['modelfile'].splitlines() if line.startswith('FROM ')
        ]
        if (
            version.get('version') != contract.runtime_version
            or len(models) != 1
            or models[0].get('digest') != contract.manifest_digest.removeprefix('sha256:')
            or sources != [contract.artifact_digest.replace(':', '-')]
            or contexts != [contract.context_length]
            or not isinstance(info.get('capabilities'), list)
            or not {'completion', 'tools'}.issubset(info['capabilities'])
        ):
            raise LLMUnavailable('local model runtime, artifact or capability identity differs')

    def identity(self, client, deadline):
        from .llm_http import request_json

        self.verify(
            *[
                request_json(client, method, self.endpoint, path, deadline, data)
                for method, path, data in (
                    ('GET', '/api/version', None),
                    ('GET', '/api/tags', None),
                    ('POST', '/api/show', {'model': self.contract.model}),
                )
            ]
        )

    async def aidentity(self, client):
        from .llm_http import arequest_json

        self.verify(
            *[
                await arequest_json(client, method, self.endpoint, path, data)
                for method, path, data in (
                    ('GET', '/api/version', None),
                    ('GET', '/api/tags', None),
                    ('POST', '/api/show', {'model': self.contract.model}),
                )
            ]
        )


def route(feature):
    # Captured upstream get_llm/get_model functions both resolve this one owner.
    from utils.byok import has_byok_keys
    from utils.llm.model_config import get_all_configured_features

    if feature not in get_all_configured_features():
        raise LLMInputRejected('unknown LLM feature is not an admitted product route')

    if has_byok_keys():
        raise LLMInputRejected('BYOK is disabled by the selected self-host model contract')
    contract = contract_for_profile()
    return (contract.model, contract.provider) if contract else ('disabled', 'disabled')


def build(model, provider, streaming=False, options=None):
    from .local_llm_chat import LocalChatModel
    from utils.llm.usage_tracker import get_usage_callback
    from .llm_usage import UsageCallback

    contract = contract_for_profile()
    if contract is None:
        if (model, provider) != ('disabled', 'disabled'):
            raise LLMInputRejected('caller changed the disabled model contract')
        return LocalChatModel(authority=None, streaming=streaming)
    if (model, provider) != (contract.model, contract.provider):
        raise LLMInputRejected('caller changed the selected local model')
    options = options or {}
    return LocalChatModel(
        authority=Authority(contract, os.environ.get('LLM_ENDPOINT', ''), timeout=contract.request_timeout_seconds),
        callbacks=[UsageCallback(get_usage_callback(), contract.model)],
        response_schema=options.get('response_schema'),
        streaming=streaming,
    )


def route_options(feature, model, provider):
    if (model, provider) != route(feature):
        raise LLMInputRejected('feature options changed the selected model')
    if feature == 'memory_l1':
        # The existing extractor supplies this exact parser contract in its
        # prompt. Constrain native generation to it; retain the original parser,
        # attribution, archive policy and canonical persistence owners.
        from utils.llm.working_observations import WorkingObservationBatch

        return {'response_schema': WorkingObservationBatch.model_json_schema()}
    return {}


def check():
    model = build(*route('chat_responses'))
    result = model.invoke('Reply with the single word ready.')
    if not result.content.strip():
        raise LLMUnavailable('local model readiness produced no text')
