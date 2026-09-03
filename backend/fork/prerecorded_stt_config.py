"""Fork-owned prerecorded-STT configuration.

These definitions lived inside upstream's ``config/prerecorded_stt.py`` on the
old shim branch. They are fork behavior -- operator-run MLX/MOSS diarization and
the private-network host rules that keep a self-hosted deployment from calling
out to a metadata service -- so they belong in a fork module, leaving the
upstream file byte-identical.

Upstream's own error and service types are imported rather than duplicated, so
this module tracks upstream instead of shadowing it.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from config.prerecorded_stt import PrerecordedSTTConfigurationError, PrerecordedSTTService


class ForkPrerecordedSTTService(PrerecordedSTTService):
    """Upstream's provider constants plus the ones only this fork serves.

    The shim branch added these three names to upstream's class, which made
    every upstream sync a conflict in a file the fork has no other reason to
    touch. Subclassing keeps one import for callers -- upstream's DEEPGRAM and
    MOSS still resolve -- while the fork-only names live in fork code.

    Values are the wire/config strings operators already set in
    ``STT_SERVICE_MODELS``, so they must not be renamed casually.
    """

    SENSEVOICE = 'sensevoice'
    MIMO = 'mimo'
    MLX_MOSS_DIARIZE = 'mlx_moss_diarize'


# Upstream keeps these private-network tables in config/prerecorded_stt.py; the
# fork re-derives them here rather than reaching into a private name.
_RFC1918_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
# Legacy integer/octal/hex IPv4 spellings that ``ipaddress`` rejects but URL
# clients and OS resolvers still accept -- e.g. 0x7f.1 or 2130706433.
_LEGACY_IPV4_LITERAL = re.compile(r'(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+)){0,3}')

_PRIVATE_HTTP_HOST_SUFFIXES = ('.internal', '.local', '.svc', '.svc.cluster.local')


_CGNAT_NETWORK = ipaddress.ip_network('100.64.0.0/10')


_ULA_NETWORK = ipaddress.ip_network('fc00::/7')


_UNSAFE_METADATA_HOSTNAMES = frozenset(
    {
        'instance-data',
        'instance-data.ec2.internal',
        'metadata',
        'metadata.aws.internal',
        'metadata.azure.internal',
        'metadata.google.internal',
    }
)


@dataclass(frozen=True)
class MlxMossDiarizeConfig:
    """Validated operator-owned mlx-audio transcription authority."""

    endpoint: str
    model: str
    api_key: str | None


_MLX_MOSS_TRANSCRIPTIONS_PATH = '/v1/audio/transcriptions'


def is_unsafe_network_hostname(hostname: str) -> bool:
    """Reject network authorities that must never receive credentials or audio."""

    normalized = hostname.rstrip('.').lower()
    if normalized in _UNSAFE_METADATA_HOSTNAMES or any(
        normalized.endswith(f'.{metadata_hostname}') for metadata_hostname in _UNSAFE_METADATA_HOSTNAMES
    ):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        # URL clients and operating-system resolvers can accept legacy integer,
        # octal, and hexadecimal IPv4 spellings that ``ipaddress`` intentionally
        # rejects. Treat numeric-looking authorities as unsafe instead of letting
        # them become operator-controlled single-label DNS names.
        return _LEGACY_IPV4_LITERAL.fullmatch(normalized) is not None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return False
    if isinstance(address, ipaddress.IPv4Address) and any(address in network for network in _RFC1918_NETWORKS):
        return False
    if isinstance(address, ipaddress.IPv6Address) and address in _ULA_NETWORK:
        return False
    return (
        address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or (isinstance(address, ipaddress.IPv4Address) and address in _CGNAT_NETWORK)
        or not address.is_global
    )


def is_private_operator_hostname(hostname: str) -> bool:
    normalized = hostname.rstrip('.').lower()
    if is_unsafe_network_hostname(normalized):
        return False
    if normalized in {'localhost', 'host.docker.internal'}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        # Single-label container/service names and reserved internal DNS suffixes
        # are operator-controlled private network authorities. Do not resolve DNS
        # in this pure configuration module.
        return '.' not in normalized or normalized.endswith(_PRIVATE_HTTP_HOST_SUFFIXES)
    if isinstance(address, ipaddress.IPv4Address):
        return address.is_loopback or any(address in network for network in _RFC1918_NETWORKS)
    return address.is_loopback or address in _ULA_NETWORK


def get_mlx_moss_diarize_config(env: Mapping[str, str] | None = None) -> MlxMossDiarizeConfig:
    """Read and validate the explicit mlx-audio endpoint/model at call time."""

    source = os.environ if env is None else env
    endpoint = (source.get('MLX_MOSS_DIARIZE_ENDPOINT') or '').strip()
    model = (source.get('MLX_MOSS_DIARIZE_MODEL') or '').strip()
    api_key = (source.get('MLX_MOSS_DIARIZE_API_KEY') or '').strip() or None
    if not endpoint:
        raise PrerecordedSTTConfigurationError(
            ForkPrerecordedSTTService.MLX_MOSS_DIARIZE,
            'MLX_MOSS_DIARIZE_ENDPOINT',
        )
    if not model:
        raise PrerecordedSTTConfigurationError(
            ForkPrerecordedSTTService.MLX_MOSS_DIARIZE,
            'MLX_MOSS_DIARIZE_MODEL',
        )

    parsed = urlsplit(endpoint)
    hostname = (parsed.hostname or '').rstrip('.').lower()
    invalid_url = (
        parsed.scheme not in {'http', 'https'}
        or not hostname
        or parsed.path != _MLX_MOSS_TRANSCRIPTIONS_PATH
        or bool(parsed.query)
        or bool(parsed.fragment)
        or parsed.username is not None
        or parsed.password is not None
    )
    if invalid_url:
        raise PrerecordedSTTConfigurationError(
            ForkPrerecordedSTTService.MLX_MOSS_DIARIZE,
            'MLX_MOSS_DIARIZE_ENDPOINT',
        )
    if (
        is_unsafe_network_hostname(hostname)
        or hostname == 'mosi.cn'
        or hostname.endswith('.mosi.cn')
        or hostname == 'omi.me'
        or hostname.endswith('.omi.me')
    ):
        raise PrerecordedSTTConfigurationError(
            ForkPrerecordedSTTService.MLX_MOSS_DIARIZE,
            'MLX_MOSS_DIARIZE_ENDPOINT',
        )
    if parsed.scheme == 'http' and not is_private_operator_hostname(hostname):
        raise PrerecordedSTTConfigurationError(
            ForkPrerecordedSTTService.MLX_MOSS_DIARIZE,
            'MLX_MOSS_DIARIZE_ENDPOINT',
        )
    if parsed.scheme == 'https' and not is_private_operator_hostname(hostname) and api_key is None:
        raise PrerecordedSTTConfigurationError(
            ForkPrerecordedSTTService.MLX_MOSS_DIARIZE,
            'MLX_MOSS_DIARIZE_API_KEY',
        )
    return MlxMossDiarizeConfig(endpoint=endpoint, model=model, api_key=api_key)
