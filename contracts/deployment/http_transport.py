"""Keep public authorities intact while rehearsing through an owned local gateway."""

import re

from urllib.parse import urlsplit


def transport_origin(metadata, service):
    origin = metadata[service + '_origin'].rstrip('/')
    probe = metadata.get('probe_origin')
    if probe is not None:
        url = urlsplit(probe)
        token = metadata.get('probe_token', '')
        if (metadata.get('gateway_origin') or url.scheme != 'https'
                or not re.fullmatch(r'eddy-ci-[bp]-[a-f0-9]{8}-probe\.[a-z0-9-]+\.workers\.dev', url.hostname or '')
                or probe != 'https://' + url.hostname or not re.fullmatch(r'[a-f0-9]{64}', token)
                or service not in ('api', 'auth', 'web')):
            raise ValueError('qualification transport requires its owned private Worker gateway')
        return probe + '/__service/' + service, {'x-release-probe': token}
    gateway = metadata.get('gateway_origin')
    if gateway is None:
        return origin, {}
    local = urlsplit(gateway)
    public = urlsplit(origin)
    if (local.scheme != 'http' or local.hostname != '127.0.0.1' or not local.port
            or local.username or local.password or local.query or local.fragment or local.path not in ('', '/')
            or public.scheme != 'https' or not public.hostname or public.username or public.password
            or public.port not in (None, 443) or public.path not in ('', '/') or public.query or public.fragment):
        raise ValueError('qualification transport requires a loopback gateway and an exact public authority')
    return gateway.rstrip('/'), {'Host': public.netloc}
