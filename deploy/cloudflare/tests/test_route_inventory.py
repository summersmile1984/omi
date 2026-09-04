"""Behavioral coverage of the fork inventory against real FastAPI routing."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI, WebSocket

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/route_inventory.py'
spec = importlib.util.spec_from_file_location('route_inventory', SCRIPT)
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


class RouteInventoryTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()

        @self.app.get('/api/items/{item_id}', include_in_schema=False)
        def get_item(item_id: str):
            return {'id': item_id}

        @self.app.websocket('/api/listen')
        async def listen(socket: WebSocket):
            await socket.accept()

        self.actual = inventory.registered_routes(self.app.routes)
        self.reviewed = inventory.reconcile_inventory(self.actual, {'routes': []})
        for row in self.reviewed['routes']:
            row.update(migration_state='staging-owned', owner='api-core', target_runtime='python-worker')

    def test_collects_hidden_http_and_websocket_registrations(self):
        self.assertEqual(
            self.actual,
            [
                {'method': 'GET', 'path': '/api/items/{item_id}', 'protocol': 'http'},
                {'method': 'WEBSOCKET', 'path': '/api/listen', 'protocol': 'websocket'},
            ],
        )
        inventory.check_inventory(self.actual, self.reviewed)

    def test_new_registered_route_fails_and_write_keeps_it_unclassified(self):
        @self.app.post('/api/new')
        def new_route():
            return {}

        actual = inventory.registered_routes(self.app.routes)
        with self.assertRaisesRegex(inventory.InventoryError, 'added POST /api/new'):
            inventory.check_inventory(actual, self.reviewed)
        updated = inventory.reconcile_inventory(actual, self.reviewed)
        with self.assertRaisesRegex(inventory.InventoryError, 'unclassified backend route: POST /api/new'):
            inventory.check_inventory(actual, updated)
        self.assertEqual(updated['routes'][0], self.reviewed['routes'][0])

    def test_removed_route_and_changed_protocol_fail(self):
        with self.assertRaisesRegex(inventory.InventoryError, 'removed WEBSOCKET /api/listen'):
            inventory.check_inventory(self.actual[:1], self.reviewed)
        self.reviewed['routes'][0]['protocol'] = 'websocket'
        with self.assertRaisesRegex(inventory.InventoryError, 'inventory is stale'):
            inventory.check_inventory(self.actual, self.reviewed)

    def test_shadowed_registration_is_one_route_but_duplicate_inventory_fails(self):
        self.app.add_api_route('/api/items/{item_id}', lambda: {}, methods=['GET'])
        self.assertEqual(inventory.registered_routes(self.app.routes), self.actual)
        self.reviewed['routes'].append(self.reviewed['routes'][0])
        with self.assertRaisesRegex(inventory.InventoryError, 'duplicate inventory entry'):
            inventory.check_inventory(self.actual, self.reviewed)

    def test_roundtrip_preserves_review_metadata_and_check_is_read_only(self):
        self.reviewed['routes'][0]['review_note'] = 'owned by the existing item service'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.json'
            path.write_text(inventory.stable_json(self.reviewed))
            before = path.read_bytes()
            current = inventory.check_inventory(self.actual, inventory.load_inventory(path))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(current['routes'][0]['review_note'], self.reviewed['routes'][0]['review_note'])

    def test_missing_owner_and_wrong_inventory_source_fail(self):
        del self.reviewed['routes'][0]['owner']
        with self.assertRaisesRegex(inventory.InventoryError, 'requires reviewed owner'):
            inventory.check_inventory(self.actual, self.reviewed)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.json'
            path.write_text('{"version": 1, "source": "unrelated.py", "routes": []}')
            with self.assertRaisesRegex(inventory.InventoryError, 'backend/main.py source'):
                inventory.load_inventory(path)


if __name__ == '__main__':
    unittest.main()
