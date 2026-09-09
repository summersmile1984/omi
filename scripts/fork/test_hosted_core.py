#!/usr/bin/env python3
"""Exercise hosted account cleanup through the production HTTP request seam."""

import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest

CORE = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'contracts/deployment/core.py'))


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(json.dumps(body).encode())
        self.code = status
        self.headers = headers or {}


class HostedCoreTests(unittest.TestCase):
    def test_https_requires_explicit_hosted_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CORE['ContractFailure']):
                CORE['ProductContract'](self.metadata(directory))
            for origin in ('https://user:pass@example.com', 'https://example.com/path', 'https://example.com?token=x'):
                with self.assertRaises(CORE['ContractFailure']):
                    CORE['ProductContract']({**self.metadata(directory), 'api_origin': origin}, remote=True)

    def metadata(self, directory):
        return {
            'target': 'self_hosted',
            'brand_id': 'eddy',
            'trace_dir': directory,
            'api_origin': 'https://api.example.com',
            'auth_origin': 'https://auth.example.com',
        }

    def test_boot_transport_keeps_the_public_auth_origin_header(self):
        with tempfile.TemporaryDirectory() as directory:
            contract = CORE['ProductContract'](
                {
                    **self.metadata(directory),
                    'api_origin': 'http://127.0.0.1:12345',
                    'api_public_origin': 'https://api.example.com',
                    'auth_origin': 'http://127.0.0.1:12346',
                    'auth_public_origin': 'https://auth.example.com',
                }
            )

            def open_request(request, timeout):
                self.assertEqual(request.full_url, 'http://127.0.0.1:12346/api/auth/get-session')
                self.assertEqual(request.get_header('Origin'), 'https://auth.example.com')
                return Response({})

            contract.opener.open = open_request
            contract.request('auth', 'GET', '/api/auth/get-session', 200)
            self.assertEqual(contract.api_public_origin, 'https://api.example.com')

    def test_only_owned_signup_is_deleted_after_fresh_signin(self):
        for deletion_status in (200, 503):
            with self.subTest(deletion_status=deletion_status), tempfile.TemporaryDirectory() as directory:
                contract = CORE['ProductContract'](self.metadata(directory), remote=True)
                requests = []
                credentials = {}

                def open_request(request, timeout):
                    requests.append((request.method, request.full_url, request.get_header('Authorization')))
                    path = request.full_url.split('.com', 1)[1]
                    if path == '/api/auth/sign-up/email':
                        credentials.update(json.loads(request.data))
                        return Response({'user': {'id': 'owned-user'}}, headers={'set-auth-token': 'owned-session'})
                    if path == '/api/auth/get-session':
                        return Response({'user': {'id': 'owned-user'}})
                    if path == '/api/auth/sign-in/email':
                        self.assertEqual(json.loads(request.data), credentials)
                        return Response({}, headers={'set-auth-token': 'renewed-session'})
                    if path == '/api/auth/token':
                        return Response({'token': 'owned.jwt.token'})
                    if path == '/v1/users/delete-account':
                        self.assertEqual(request.method, 'DELETE')
                        self.assertEqual(request.get_header('Authorization'), 'Bearer owned.jwt.token')
                        return Response({}, status=deletion_status)
                    self.fail('unexpected HTTP request')

                contract.opener.open = open_request
                contract.signup()
                report = contract.report()
                self.assertEqual(report['passed'], deletion_status == 200)
                self.assertEqual(sum(method == 'DELETE' for method, _, _ in requests), 1)
                contract.report()
                self.assertEqual(sum(method == 'DELETE' for method, _, _ in requests), 1)
                trace = contract.trace.read_text()
                self.assertNotIn(credentials['password'], trace)
                self.assertNotIn(credentials['email'], trace)
                self.assertNotIn('owned.jwt.token', trace)


if __name__ == '__main__':
    unittest.main()
