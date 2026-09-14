"""Public brand identity baked from the same manifest as the deployment profile."""

from dataclasses import asdict, dataclass
from functools import lru_cache
import json

from . import profile


@dataclass(frozen=True)
class Brand:
    brand_id: str
    display_name: str
    ai_persona_name: str

    @classmethod
    def parse(cls, value, *, brand_id):
        if not isinstance(value, dict) or set(value) != {'brand_id', 'display_name', 'ai_persona_name'}:
            raise profile.ProfileError('brand runtime requires the public identity contract')
        if any(not isinstance(item, str) or not item.strip() for item in value.values()):
            raise profile.ProfileError('brand runtime names must be nonempty strings')
        if any(ord(character) < 32 or ord(character) == 127 for item in value.values() for character in item):
            raise profile.ProfileError('brand runtime names cannot contain control characters')
        if value['brand_id'] != brand_id:
            raise profile.ProfileError('brand runtime does not match the selected profile brand')
        return cls(**value)

    def as_dict(self):
        return asdict(self)


def from_manifest(manifest, table):
    """The caller validates the canonical manifest; no independent name settings."""
    source = manifest['brand']
    return Brand.parse(
        {
            'brand_id': source['id'],
            'display_name': source['display_name'],
            'ai_persona_name': source['ai_persona_name'],
        },
        brand_id=table['brand'],
    )


@lru_cache(maxsize=1)
def current():
    table = profile._load_table()
    path = profile.GENERATED_TABLE.with_name('brand.runtime.json')
    try:
        # Identity is public, bounded build metadata, never an operator-supplied
        # prompt or a runtime network lookup. Missing old artifacts fail clearly.
        with path.open('rb') as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError('oversized identity')
        value = json.loads(raw)
    except (OSError, ValueError) as error:
        raise profile.ProfileError('missing or invalid brand.runtime.json; rebuild the selected brand image') from error
    return Brand.parse(value, brand_id=table['brand'])
