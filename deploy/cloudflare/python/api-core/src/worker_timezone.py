"""Load IANA TZif data from the Worker's pinned package, without a host OS database."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytz


def load_zoneinfo(key: str) -> ZoneInfo:
    # Exact registry membership keeps ZoneInfo's case-sensitive IANA contract
    # and rejects filesystem paths before opening a packaged resource.
    if key not in pytz.all_timezones_set:
        raise ZoneInfoNotFoundError(key)
    with pytz.open_resource(key) as resource:
        return ZoneInfo.from_file(resource, key=key)
