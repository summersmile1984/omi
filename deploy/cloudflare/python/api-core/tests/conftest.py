"""Match the fixed builder's ordinary shared-module source projection in CPython."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[2] / 'shared'))
