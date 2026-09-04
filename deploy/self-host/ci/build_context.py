#!/usr/bin/env python3
"""Exercise both runtime Docker context filters with an actual offline build.

LIFECYCLE: permanent
The 2026-09-04 image builds copied the local OpenAPI environment and test scratch
directory into the serving image, adding hundreds of megabytes per source layer.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
FILTERS = ("backend/Dockerfile.dockerignore", "deploy/self-host/Dockerfile.dockerignore")
RUNTIME_FILES = ("backend/fork/model_contract.py", "backend/firestore_pg/migrations.py")
CACHE_FILES = (
    "backend/.openapi-venv/lib/local-only.py",
    "backend/_temp/probe-response.json",
    "backend/.venv/lib/local-only.py",
    "backend/fork/__pycache__/local-only.pyc",
    "backend/.env",
)


class RuntimeBuildContext(unittest.TestCase):
    def test_actual_docker_filters_keep_runtime_sources_and_exclude_local_validation_state(self):
        for relative in FILTERS:
            with self.subTest(context_filter=relative), tempfile.TemporaryDirectory(prefix="fork-context-") as temp:
                directory = Path(temp)
                source, output = directory / "source", directory / "output"
                dockerfile = source / relative.removesuffix(".dockerignore")
                dockerfile.parent.mkdir(parents=True)
                dockerfile.write_text("FROM scratch\nCOPY backend/ /backend/\n")
                shutil.copyfile(ROOT / relative, source / relative)
                for name in RUNTIME_FILES:
                    destination = source / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / name, destination)
                for name in CACHE_FILES:
                    destination = source / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text("synthetic local validation state\n")
                result = subprocess.run(
                    [
                        "docker",
                        "build",
                        "--network=none",
                        "--pull=false",
                        "--file",
                        str(dockerfile),
                        "--output",
                        f"type=local,dest={output}",
                        str(source),
                    ],
                    env={**os.environ, "DOCKER_BUILDKIT": "1", "BUILDX_NO_DEFAULT_ATTESTATIONS": "1"},
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr[-3000:])
                for name in RUNTIME_FILES:
                    self.assertEqual((output / name).read_bytes(), (ROOT / name).read_bytes())
                for name in CACHE_FILES:
                    self.assertFalse((output / name).exists(), f"{relative} shipped local state: {name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
