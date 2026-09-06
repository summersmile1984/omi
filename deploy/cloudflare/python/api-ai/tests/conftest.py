"""Match the fixed builder's ordinary shared-module source projection in CPython."""

from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parents[2] / 'shared'))

# The cross-Worker chat cancellation tests execute Core's real clear routes.
# Load their ordinary generated feedback contracts too; API AI does not ship
# Core's feedback routes or this temporary test-only projection.
_core_stage = TemporaryDirectory(prefix='cf-ai-test-core-domain-')
runpy.run_path(str(Path(__file__).parents[3] / 'scripts/feedback_sources.py'))['generate'](Path(_core_stage.name))
sys.path.insert(0, _core_stage.name)


def pytest_unconfigure(config):
    sys.path.remove(_core_stage.name)
    _core_stage.cleanup()
