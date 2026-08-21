import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

RUNTIME_GOOGLE_CREDENTIALS_PATH = Path('/tmp/omi-google-credentials.json')


def _require_private_credentials_file(path: Path, label: str) -> Path:
    """Return a credential file only when its bytes are owner-readable."""

    if path.is_symlink():
        raise RuntimeError(f'{label} must be a regular file, not a symlink: {path}')
    if not path.exists():
        raise RuntimeError(f'{label} points to missing file: {path}')
    if not path.is_file():
        raise RuntimeError(f'{label} must be a regular file, not a symlink: {path}')
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError(f'{label} must be mode 0600 or stricter: {path}')
    return path


def prepare_google_credentials() -> None:
    service_account_json = os.environ.get('SERVICE_ACCOUNT_JSON', '').strip()
    if service_account_json:
        _write_credentials_file(service_account_json)
        return

    credentials = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', '').strip()
    if not credentials:
        return

    if credentials.startswith('{'):
        _write_credentials_file(credentials)
        return

    credentials_path = Path(credentials)
    _require_private_credentials_file(credentials_path, 'GOOGLE_APPLICATION_CREDENTIALS')


def customer_data_service_account() -> tuple[Any, str] | None:
    """Return explicit customer-data credentials when ``SERVICE_ACCOUNT_JSON`` is set.

    Dev GKE listen mounts both Workload Identity (parity-pack exporter) and the
    runtime JSON SA (nik-164 / prod customer project). ADC can silently prefer
    the pack WI identity, and ``GOOGLE_CLOUD_PROJECT`` can point at the compute
    project (``based-hardware-dev``) while customer Firestore lives in
    ``based-hardware``. Callers that read user docs must pin the JSON SA and its
    ``project_id`` the same way Firebase Admin and GCS already do.
    """
    prepare_google_credentials()
    service_account_json = os.environ.get('SERVICE_ACCOUNT_JSON', '').strip()
    if not service_account_json:
        return None

    try:
        service_account_info = json.loads(service_account_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError('Google service account credentials are not valid JSON') from exc

    if not isinstance(service_account_info, dict):
        raise RuntimeError('SERVICE_ACCOUNT_JSON must decode to a JSON object')

    project_id = service_account_info.get('project_id')
    if not isinstance(project_id, str) or not project_id.strip():
        raise RuntimeError('SERVICE_ACCOUNT_JSON is missing project_id')

    # Imported lazily so unit harnesses that stub ``google`` keep working until
    # a caller actually needs customer-data credentials.
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(service_account_info)  # type: ignore[reportUnknownMemberType]  # google.oauth2 partial stubs
    return credentials, project_id.strip()


def customer_entitlement_service_account() -> tuple[Any, str] | None:
    """Customer-data credentials for entitlements without retargeting ADC.

    Listen/Python set ``SERVICE_ACCOUNT_JSON`` and pin every Firestore client.
    Development desktop-backend mounts that same SA only at
    ``FIREBASE_AUTH_CREDENTIALS_PATH`` so Cloud Run ADC stays on
    ``GOOGLE_CLOUD_PROJECT`` (GCE / ``agentVm``). Quota, usage, and
    subscription reads must still use the SA's ``project_id``.
    """
    pinned = customer_data_service_account()
    if pinned is not None:
        return pinned

    credentials_path = os.environ.get('FIREBASE_AUTH_CREDENTIALS_PATH', '').strip()
    if not credentials_path:
        return None

    path = _require_private_credentials_file(Path(credentials_path), 'FIREBASE_AUTH_CREDENTIALS_PATH')

    try:
        service_account_info = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise RuntimeError('FIREBASE_AUTH_CREDENTIALS_PATH is not valid JSON') from exc

    if not isinstance(service_account_info, dict):
        raise RuntimeError('FIREBASE_AUTH_CREDENTIALS_PATH must decode to a JSON object')

    project_id = service_account_info.get('project_id')
    if not isinstance(project_id, str) or not project_id.strip():
        raise RuntimeError('FIREBASE_AUTH_CREDENTIALS_PATH is missing project_id')

    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(service_account_info)  # type: ignore[reportUnknownMemberType]  # google.oauth2 partial stubs
    return credentials, project_id.strip()


def _write_credentials_file(raw_credentials: str) -> None:
    try:
        service_account_info = json.loads(raw_credentials)
    except json.JSONDecodeError as exc:
        raise RuntimeError('Google service account credentials are not valid JSON') from exc

    fchmod = getattr(os, 'fchmod', None)
    if not callable(fchmod):
        raise RuntimeError('Google service account credentials require owner-only file permissions')

    RUNTIME_GOOGLE_CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=RUNTIME_GOOGLE_CREDENTIALS_PATH.parent,
        prefix=f'.{RUNTIME_GOOGLE_CREDENTIALS_PATH.name}.',
        text=True,
    )
    try:
        fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            fd = -1
            json.dump(service_account_info, handle)
        os.replace(temp_path, RUNTIME_GOOGLE_CREDENTIALS_PATH)
    except Exception:
        if fd >= 0:
            os.close(fd)
        Path(temp_path).unlink(missing_ok=True)
        raise
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = str(RUNTIME_GOOGLE_CREDENTIALS_PATH)
