"""Tests for the static startup source-closure checker (not runtime coverage)."""

from __future__ import annotations

import runpy
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHECK = runpy.run_path(str(ROOT / 'deploy/self-host/check-config.py'))


@pytest.fixture
def source(tmp_path):
    for name in (*CHECK['REQUIRED_SOURCE'], 'deploy/self-host/compose.production.yml', 'auth-server/Dockerfile'):
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    return tmp_path


def test_production_compose_source_closure_is_complete():
    CHECK['check_sources']()


def test_missing_migration_module_fails(source):
    (source / 'backend/fork/migrate.py').unlink()
    with pytest.raises(ValueError, match='startup source missing'):
        CHECK['check_sources'](source)


def test_typo_in_compose_command_fails(source):
    path = source / 'deploy/self-host/compose.production.yml'
    config = yaml.safe_load(path.read_text())
    config['services']['firestore-pg-migrate']['command'] = ['python', '-m', 'fork.not_a_migration']
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match='Python entrypoint missing'):
        CHECK['check_sources'](source)


def test_example_environment_cannot_admit_a_deployment():
    with pytest.raises(ValueError, match='not reviewed'):
        CHECK['check_environment'](ROOT / 'deploy/self-host/.env.production.example')
