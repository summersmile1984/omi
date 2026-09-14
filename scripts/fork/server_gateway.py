#!/usr/bin/env python3
"""Render and run the Server gateway from the accepted source's public profile."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


NGINX_IMAGE = 'nginx:1.28-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236'


def gateway_config(profile, nginx_image, web_image, port, *, tls=False):
    if not re.fullmatch(r'nginx:[\w.-]+@sha256:[a-f0-9]{64}', nginx_image):
        raise ValueError('gateway requires a pinned official nginx image')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', web_image) or not 1024 <= port <= 65535:
        raise ValueError('gateway requires an accepted Web image ID and an unprivileged loopback port')
    hosts = {}
    for key in ('api', 'auth', 'web', 'objects'):
        url = urlsplit(profile[f'{key}_base_url'])
        if (
            url.scheme != 'https'
            or not url.hostname
            or url.username
            or url.password
            or url.path not in ('', '/')
            or url.query
            or url.fragment
            or url.port not in (None, 443)
        ):
            raise ValueError('gateway requires public HTTPS origins from the selected profile')
        if not re.fullmatch(r'[a-z0-9.-]+', url.hostname):
            raise ValueError('invalid gateway hostname')
        hosts[key] = url.hostname
    if len(set(hosts.values())) != 4:
        raise ValueError('Server gateway surfaces must have distinct hostnames')
    blocks = []
    tls_listener = '''
    listen 443 ssl;
    ssl_certificate /etc/nginx/tls/server.crt;
    ssl_certificate_key /etc/nginx/tls/server.key;''' if tls else ''
    for key, upstream in {
        'api': 'backend:8080',
        'auth': 'auth-server:3000',
        'web': 'web:3000',
        'objects': 'minio:9000',
    }.items():
        blocks.append(f'''
  server {{
    listen 8080; server_name {hosts[key]};{tls_listener}
    client_max_body_size 100m;
    location ^~ /internal/ {{ return 404; }}
    location / {{
      set $upstream_target {upstream};
      proxy_pass http://$upstream_target;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-Host $host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_buffering off;
      proxy_request_buffering off;
      proxy_read_timeout 300s;
    }}
  }}''')
    nginx = '''events { worker_connections 1024; }
http {
  resolver 127.0.0.11 valid=5s ipv6=off;
  access_log off;
  map $http_upgrade $connection_upgrade { default upgrade; '' close; }
  server { listen 8080 default_server; server_name _; return 404; }
''' + '\n'.join(blocks) + '\n}\n'
    compose = {
        'services': {
            'web': {'image': web_image, 'restart': 'unless-stopped', 'environment': {'PORT': '3000'}},
            'gateway': {
                'image': nginx_image,
                'restart': 'unless-stopped',
                'ports': [f'127.0.0.1:{port}:8080'],
                'volumes': ['./nginx.conf:/etc/nginx/nginx.conf:ro'],
            },
        }
    }
    return nginx, compose, hosts


def qualification_gateway(directory, profile, nginx_image, web_image, port):
    """Use the real gateway with private DNS/TLS inside the disposable network."""
    import certifi

    nginx, compose, hosts = gateway_config(profile, nginx_image, web_image, port, tls=True)
    certificate, key = directory / 'server.crt', directory / 'server.key'
    subprocess.run([
        'openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:2048', '-days', '1',
        '-subj', '/CN=Release qualification', '-addext',
        'subjectAltName=' + ','.join('DNS:' + host for host in hosts.values()),
        '-keyout', str(key), '-out', str(certificate),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    ca = directory / 'ca-bundle.pem'
    ca.write_bytes(Path(certifi.where()).read_bytes() + b'\n' + certificate.read_bytes())
    ca.chmod(0o644)
    gateway = compose['services']['gateway']
    gateway['networks'] = {'default': {'aliases': list(hosts.values())}}
    gateway['volumes'] = [f'{directory / "nginx.conf"}:/etc/nginx/nginx.conf:ro',
                          f'{certificate}:/etc/nginx/tls/server.crt:ro', f'{key}:/etc/nginx/tls/server.key:ro']
    for service in ('backend', 'auth-server', 'queue-worker', 'memory-maintenance-worker', 'web'):
        target = compose['services'].setdefault(service, {})
        target.setdefault('environment', {}).update({name: '/etc/ssl/release-ca.pem' for name in
            ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS')})
        target.setdefault('volumes', []).append(f'{ca}:/etc/ssl/release-ca.pem:ro')
    (directory / 'nginx.conf').write_text(nginx)
    path = directory / 'gateway-compose.json'
    path.write_text(json.dumps(compose))
    return path, {
        'gateway_origin': f'http://127.0.0.1:{port}',
        **{f'{role}_origin': 'https://' + hosts[role] for role in ('api', 'auth', 'web')},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True, type=Path)
    args = parser.parse_args()
    release = args.release.resolve()
    receipt = json.loads((release / 'delivery.json').read_text())
    destination = release.parent.parent
    settings = json.loads((destination / 'gateway.json').read_text())
    rendered = subprocess.run(
        [
            sys.executable,
            str(release / 'source/deploy/web/profile_input.py'),
            '--target',
            'self_hosted',
            '--brand',
            receipt['brand'],
            '--stage',
            receipt['stage'],
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    profile = json.loads(rendered.stdout)['profile']
    nginx, compose, hosts = gateway_config(
        profile, settings['nginx_image'], receipt['images']['web']['image_id'], settings['port']
    )
    (release / 'nginx.conf').write_text(nginx)
    path = release / 'gateway-compose.json'
    path.write_text(json.dumps(compose, indent=2))
    command = ['docker', 'compose', '--project-name', f'eddy-server-{receipt["stage"]}', '--file', str(path)]
    subprocess.run([*command, 'up', '--detach', '--no-build', '--wait', 'web'], check=True)
    subprocess.run([*command, 'run', '--rm', '--no-deps', 'gateway', 'nginx', '-t'], check=True)
    subprocess.run([*command, 'up', '--detach', '--no-build', '--wait'], check=True)
    metadata = {
        'target': 'self_hosted',
        'brand_id': receipt['brand'],
        'trace_dir': str(release / 'acceptance'),
        **{f'{key}_origin': 'https://' + hosts[key] for key in ('api', 'auth', 'web')},
    }
    (release / 'metadata.json').write_text(json.dumps(metadata))


if __name__ == '__main__':
    main()
