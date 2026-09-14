"""Public brand identity projected from the validated manifest at build time."""

from dataclasses import dataclass
import json
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BrandRuntime:
    brand_id: str
    display_name: str
    ai_persona_name: str


def load_brand_runtime(env: object) -> BrandRuntime:
    """Reject unconfigured fork deployments; never infer an upstream identity."""
    raw = getattr(env, 'BRAND_RUNTIME_JSON', None)
    if not isinstance(raw, str):
        raise ValueError('brand runtime is not configured')
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('brand runtime is not configured') from None
    if (
        not isinstance(value, dict)
        or set(value) != {'brand_id', 'display_name', 'ai_persona_name'}
        or any(not isinstance(item, str) or not item.strip() for item in value.values())
        or not re.fullmatch(r'[a-z0-9-]+', value['brand_id'])
        or any(ord(char) < 32 or ord(char) == 127 for item in value.values() for char in item)
    ):
        raise ValueError('brand runtime is not configured')
    return BrandRuntime(**value)


def load_support_email(env: object) -> str:
    """Read the plain public contact projected from the same brand manifest."""
    value = getattr(env, 'BRAND_SUPPORT_EMAIL', None)
    if (
        not isinstance(value, str)
        or not re.fullmatch(r'[^\s@<>(),:;\[\]\\"]+@[^\s@<>(),:;\[\]\\"]+', value)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError('brand support contact is not configured')
    return value


def load_share_origin(env: object) -> str:
    """Read the public Web origin used to mint share capabilities."""
    value = getattr(env, 'PUBLIC_SHARE_BASE_URL', None)
    if not isinstance(value, str):
        raise ValueError('public share origin is not configured')
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError('public share origin is not configured') from error
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {'', '/'}
    ):
        raise ValueError('public share origin is not configured')
    authority = parsed.hostname
    if ':' in authority and not authority.startswith('['):
        authority = f'[{authority}]'
    if port is not None:
        authority = f'{authority}:{port}'
    return f'{parsed.scheme}://{authority}'
