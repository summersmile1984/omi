"""Exercise the production entrypoint with controllable application imports."""

import asyncio
import builtins
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    workers = ModuleType('workers')
    workers.WorkerEntrypoint = object
    control = ModuleType('bootstrap_control')
    control.imports = 0
    control.fail = False
    control.framework_loaded = False
    original_import = builtins.__import__

    def import_module(name, *args, **kwargs):
        if name == 'fastapi':
            control.framework_loaded = True
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', import_module)
    monkeypatch.setitem(sys.modules, 'workers', workers)
    monkeypatch.setitem(sys.modules, 'bootstrap_control', control)
    monkeypatch.delitem(sys.modules, '_worker_application', raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / '_worker_application.py').write_text(
        'import bootstrap_control as control\n'
        'assert control.framework_loaded\n'
        'control.imports += 1\n'
        'if control.fail: raise RuntimeError("application initialization failed")\n'
        'class Default:\n'
        '    async def fetch(self, request):\n'
        '        return request, self.env, self.ctx\n'
    )
    namespace = runpy.run_path(str(Path(__file__).parents[2] / 'core_entrypoint.py'))
    worker = namespace['Default']()
    worker.env = object()
    worker.ctx = object()
    yield worker, control
    sys.modules.pop('_worker_application', None)


def test_application_loads_on_request_and_preserves_asgi_owner(bootstrap):
    worker, control = bootstrap
    assert control.imports == 0
    assert control.framework_loaded
    for kind in ('http', 'websocket'):
        request = SimpleNamespace(kind=kind)
        result = asyncio.run(worker.fetch(request))
        assert result == (request, worker.env, worker.ctx)
    assert control.imports == 1


def test_failed_application_import_propagates_and_can_retry(bootstrap):
    worker, control = bootstrap
    control.fail = True
    with pytest.raises(RuntimeError, match='application initialization failed'):
        asyncio.run(worker.fetch(object()))
    assert control.imports == 1
    assert '_worker_application' not in sys.modules
    control.fail = False
    request = object()
    assert asyncio.run(worker.fetch(request)) == (request, worker.env, worker.ctx)
    assert control.imports == 2
