#!/usr/bin/env python3
"""Controlled embedding protocol for hermetic product tests; no model inference.

The real backend HTTP adapter still checks model identity, dimensions and wire
shape. This fixture is never shipped as an inference provider or release proof.
"""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

CONTRACT = json.loads(Path('/contract/profile.json').read_text())['profiles']['self_hosted.local']['embedding']


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/api/tags':
            self.reply(
                200,
                {
                    'models': [
                        {'name': CONTRACT['model'], 'digest': CONTRACT['manifest_digest'].removeprefix('sha256:')}
                    ]
                },
            )
        else:
            self.reply(503, {'error': 'controlled_fixture_has_no_provider_for_this_route'})

    def do_POST(self):
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= 1024 * 1024:
            self.reply(400, {'error': 'invalid_input'})
            return
        body = json.loads(self.rfile.read(size))
        if body.get('model') != CONTRACT['model']:
            self.reply(400, {'error': 'unselected_model'})
        elif self.path == '/api/show':
            self.reply(
                200,
                {
                    'model_info': {
                        'fixture.embedding_length': CONTRACT['dimension'],
                        'fixture.context_length': CONTRACT['context_length'],
                    },
                    'modelfile': 'FROM /controlled-fixture/' + CONTRACT['artifact_digest'].replace(':', '-'),
                    'capabilities': ['embedding'],
                },
            )
        elif self.path == '/api/embed' and isinstance(body.get('input'), list):
            vectors = []
            for value in body['input']:
                digest = hashlib.sha256(value.encode()).digest()
                vectors.append([1.0 + digest[i % len(digest)] / 255 for i in range(CONTRACT['dimension'])])
            self.reply(200, {'model': CONTRACT['model'], 'embeddings': vectors})
        else:
            self.reply(503, {'error': 'controlled_fixture_has_no_provider_for_this_route'})


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 11434), Provider).serve_forever()
