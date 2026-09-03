"""Object storage: GCS -> MinIO for operator-run deployments.

Upstream's `utils.other.storage._get_storage_client()` builds a Google Cloud
Storage client and is called from every bucket helper in that module. Replacing
that one factory at boot redirects all of them, so no upstream call site changes
and no upstream file is edited.
"""

from __future__ import annotations

from typing import Any, Callable, List

from ..registry import Patch


def _uses_minio(profile: dict) -> bool:
    return profile.get("data_plane", {}).get("object_store") == "minio"


def _minio_client_factory(original: Callable[..., Any]) -> Callable[..., Any]:
    def factory(*args: Any, **kwargs: Any) -> Any:
        from ..storage_minio import get_minio_client

        return get_minio_client()

    factory.__doc__ = (
        "fork: MinIO client standing in for GCS. Replaces "
        f"{getattr(original, '__module__', '?')}.{getattr(original, '__name__', '?')}."
    )
    return factory


def patches() -> List[Patch]:
    return [
        Patch(
            name="storage.minio-client",
            module="utils.other.storage",
            attribute="_get_storage_client",
            build=_minio_client_factory,
            applies_to=_uses_minio,
            reason="operator-run deployments have no GCS; MinIO speaks the same S3-shaped API",
        )
    ]
