"""Match the fixed builder's ordinary shared-module source projection in CPython."""

from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parents[2] / 'shared'))

# Tests import the same generated domain modules as the ordinary Worker build.
_screen_stage = TemporaryDirectory(prefix='cf-screen-domain-')
runpy.run_path(str(Path(__file__).parents[3] / 'scripts/screen_frame_sources.py'))['generate'](Path(_screen_stage.name))
runpy.run_path(str(Path(__file__).parents[3] / 'scripts/frame_request_sources.py'))['generate'](
    Path(_screen_stage.name)
)
sys.path.insert(0, _screen_stage.name)


def pytest_unconfigure(config):
    sys.path.remove(_screen_stage.name)
    _screen_stage.cleanup()
