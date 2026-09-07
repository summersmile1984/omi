"""Use the same encoded URL pathname that the Worker assertion issuer signs."""


def raw_request_path(scope: dict) -> str | None:
    # ASGI path is percent-decoded; Workers SDK supplies the original URL
    # pathname as raw_path. Missing/malformed raw bytes cannot authenticate.
    raw = scope.get('raw_path')
    if not isinstance(raw, bytes):
        return None
    try:
        return raw.decode('ascii')
    except UnicodeDecodeError:
        return None
