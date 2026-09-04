"""Public brand identity projected from the validated manifest at build time."""

from dataclasses import dataclass
import json
import re


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
