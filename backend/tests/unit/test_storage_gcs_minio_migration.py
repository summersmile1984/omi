from __future__ import annotations

import io
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterable

import pytest

from scripts import storage_gcs_minio_migration as migration


@dataclass
class _SourceValue:
    generation: str
    content: bytes
    metadata: dict[str, str]
    content_type: str | None


@dataclass
class _TargetValue:
    content: bytes
    metadata: dict[str, str]
    content_type: str | None


class _FakeSource:
    def __init__(self) -> None:
        self._authority = migration.SourceAuthority(
            project='production-source-project',
            endpoint='https://storage.googleapis.com',
        )
        self.objects: dict[tuple[str, str], _SourceValue] = {}

    @property
    def authority(self) -> migration.SourceAuthority:
        return self._authority

    def add(
        self,
        bucket: str,
        name: str,
        content: bytes,
        *,
        generation: str = '1',
        metadata: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> None:
        self.objects[(bucket, name)] = _SourceValue(
            generation=generation,
            content=content,
            metadata=dict(metadata or {}),
            content_type=content_type,
        )

    def list_objects(self, scope: migration.Scope) -> Iterable[migration.SourceDescriptor]:
        for (bucket, name), value in sorted(self.objects.items()):
            if bucket == scope.source_bucket and name.startswith(scope.source_prefix):
                yield migration.SourceDescriptor(
                    bucket=bucket,
                    name=name,
                    generation=value.generation,
                    size=len(value.content),
                    metadata=dict(value.metadata),
                    content_type=value.content_type,
                )

    def open_object(self, record: migration.ObjectRecord) -> BinaryIO:
        value = self.objects[(record.source_bucket, record.source_name)]
        if value.generation != record.generation:
            raise migration.StorageReconciliationError('source generation precondition failed')
        return io.BytesIO(value.content)


class _FakeTarget:
    def __init__(self) -> None:
        self._authority = 'http://minio:9000'
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], _TargetValue] = {}
        self.fail_after_store_once = False
        self.after_put: Callable[[], None] | None = None
        self.put_calls = 0

    @property
    def authority(self) -> str:
        return self._authority

    def ensure_bucket(self, bucket: str) -> None:
        self.buckets.add(bucket)

    def list_objects(self, scope: migration.Scope) -> Iterable[migration.TargetDescriptor]:
        for (bucket, name), value in sorted(self.objects.items()):
            if bucket == scope.target_bucket and name.startswith(scope.target_prefix):
                yield migration.TargetDescriptor(
                    bucket=bucket,
                    name=name,
                    size=len(value.content),
                    metadata=dict(value.metadata),
                    content_type=value.content_type,
                )

    def head_object(self, bucket: str, name: str) -> migration.TargetDescriptor | None:
        value = self.objects.get((bucket, name))
        if value is None:
            return None
        return migration.TargetDescriptor(
            bucket=bucket,
            name=name,
            size=len(value.content),
            metadata=dict(value.metadata),
            content_type=value.content_type,
        )

    def open_object(self, bucket: str, name: str) -> BinaryIO:
        return io.BytesIO(self.objects[(bucket, name)].content)

    def put_object_create_only(
        self,
        record: migration.ObjectRecord,
        stream: BinaryIO,
        *,
        plan_sha256: str,
    ) -> None:
        self.put_calls += 1
        key = (record.target_bucket, record.target_name)
        if key in self.objects:
            raise migration.TargetConflictError('target object already exists')
        chunks: list[bytes] = []
        while chunk := stream.read(3):
            chunks.append(chunk)
        self.objects[key] = _TargetValue(
            content=b''.join(chunks),
            metadata=migration._expected_target_metadata(record, plan_sha256),
            content_type=record.content_type,
        )
        if self.after_put is not None:
            self.after_put()
        if self.fail_after_store_once:
            self.fail_after_store_once = False
            raise RuntimeError('controlled interruption after durable target write')


def _write_plan(
    path: Path,
    *,
    scopes: list[dict[str, str]] | None = None,
) -> migration.MigrationPlan:
    payload = {
        'schema_version': 1,
        'scopes': scopes
        or [
            {
                'id': 'speech-profiles',
                'source_bucket': 'production-speech-profiles',
                'source_prefix': '',
                'target_bucket': 'omi-speech-profiles',
                'target_prefix': '',
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding='utf-8')
    return migration.load_plan(path)


def _captured(tmp_path: Path) -> tuple[_FakeSource, migration.MigrationPlan, migration.Manifest]:
    source = _FakeSource()
    source.add(
        'production-speech-profiles',
        'tenant-a/speech_profile.wav',
        b'wave-bytes-a',
        generation='41',
        metadata={'owner-kind': 'speech-profile'},
        content_type='audio/wav',
    )
    source.add(
        'production-speech-profiles',
        'tenant-b/speech_profile.wav',
        b'wave-bytes-b',
        generation='42',
        content_type='audio/wav',
    )
    plan = _write_plan(tmp_path / 'plan.json')
    manifest = migration.capture_inventory(source, plan, tmp_path / 'inventory.jsonl')
    return source, plan, manifest


def test_entrypoint_resolves_backend_packages_from_any_working_directory(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'storage_gcs_minio_migration.py'

    result = subprocess.run(
        [sys.executable, str(script), '--help'],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert 'Fail-closed, resumable GCS/Firebase Storage to MinIO migration' in result.stdout
    assert '{dry-run,apply,verify}' in result.stdout


@pytest.mark.parametrize(
    'scopes,match',
    [
        (
            [
                {
                    'id': 'unsafe',
                    'source_bucket': 'source-bucket',
                    'source_prefix': '../tenant/',
                    'target_bucket': 'target-bucket',
                    'target_prefix': 'tenant/',
                }
            ],
            'source prefix is unsafe',
        ),
        (
            [
                {
                    'id': 'tenant-a',
                    'source_bucket': 'source-bucket',
                    'source_prefix': 'tenants/',
                    'target_bucket': 'target-bucket',
                    'target_prefix': 'users/',
                },
                {
                    'id': 'tenant-b',
                    'source_bucket': 'source-bucket',
                    'source_prefix': 'tenants/a/',
                    'target_bucket': 'target-other',
                    'target_prefix': 'users/a/',
                },
            ],
            'source scopes overlap',
        ),
    ],
)
def test_plan_rejects_path_escape_and_overlapping_tenant_scopes(
    tmp_path: Path,
    scopes: list[dict[str, str]],
    match: str,
) -> None:
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps({'schema_version': 1, 'scopes': scopes}), encoding='utf-8')

    with pytest.raises(migration.StorageMigrationError, match=match):
        migration.load_plan(path)


def test_capture_inventory_records_generation_hash_metadata_and_private_mode(tmp_path: Path) -> None:
    source, plan, manifest = _captured(tmp_path)

    loaded = migration.load_manifest(manifest.path, plan)

    assert loaded.inventory.count == 2
    assert loaded.inventory.content_hash == manifest.inventory.content_hash
    assert loaded.source_authority == source.authority
    assert loaded.records[0].generation == '41'
    assert loaded.records[0].size == len(b'wave-bytes-a')
    assert loaded.records[0].sha256 == migration.hashlib.sha256(b'wave-bytes-a').hexdigest()
    assert loaded.records[0].metadata == {'owner-kind': 'speech-profile'}
    assert loaded.records[0].content_type == 'audio/wav'
    assert manifest.path.stat().st_mode & 0o777 == 0o600


def test_resume_rejects_non_private_manifest_and_checkpoint(tmp_path: Path) -> None:
    source, plan, manifest = _captured(tmp_path)
    checkpoint_path = tmp_path / 'checkpoint.json'
    target = _FakeTarget()
    migration.run_apply(source, target, plan, manifest, checkpoint_path, existing_policy='create-only')

    manifest.path.chmod(0o644)
    with pytest.raises(migration.StorageMigrationError, match='inventory manifest.*0600'):
        migration.load_manifest(manifest.path, plan)

    manifest.path.chmod(0o600)
    checkpoint_path.chmod(0o644)
    with pytest.raises(migration.StorageMigrationError, match='migration checkpoint.*0600'):
        migration.run_verify(
            source,
            target,
            plan,
            manifest,
            checkpoint_path,
            existing_policy='create-only',
        )


def test_apply_streams_create_only_objects_then_verify_reconciles_fresh_source_and_target(tmp_path: Path) -> None:
    source, plan, manifest = _captured(tmp_path)
    target = _FakeTarget()
    checkpoint_path = tmp_path / 'checkpoint.json'

    applied = migration.run_apply(
        source,
        target,
        plan,
        manifest,
        checkpoint_path,
        existing_policy='create-only',
    )

    assert applied['status'] == 'applied'
    assert applied['next_index'] == 2
    result = migration.run_verify(
        source,
        target,
        plan,
        manifest,
        checkpoint_path,
        existing_policy='create-only',
    )
    assert result['status'] == 'passed'
    assert result['source_live_count'] == result['target_count'] == 2
    assert result['source_live_content_hash'] == result['target_content_hash'] == manifest.inventory.content_hash
    assert target.put_calls == 2
    assert target.objects[('omi-speech-profiles', 'tenant-a/speech_profile.wav')].content == b'wave-bytes-a'
    assert checkpoint_path.stat().st_mode & 0o777 == 0o600


def test_apply_rechecks_source_guard_before_each_source_object_read(tmp_path: Path) -> None:
    source, plan, manifest = _captured(tmp_path)
    target = _FakeTarget()
    checkpoint_path = tmp_path / 'checkpoint.json'
    calls = 0

    def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise migration.SourceWriteFreezeError('source-write freeze lease expired')

    with pytest.raises(migration.SourceWriteFreezeError, match='lease expired'):
        migration.run_apply(
            source,
            target,
            plan,
            manifest,
            checkpoint_path,
            existing_policy='create-only',
            source_read_guard=guard,
        )

    assert calls == 2
    assert target.put_calls == 1
    checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
    assert checkpoint['status'] == 'failed'
    assert checkpoint['next_index'] == 1


def test_verify_rechecks_source_guard_before_passing_cutover_evidence(tmp_path: Path) -> None:
    source, plan, manifest = _captured(tmp_path)
    target = _FakeTarget()
    checkpoint_path = tmp_path / 'checkpoint.json'
    migration.run_apply(
        source,
        target,
        plan,
        manifest,
        checkpoint_path,
        existing_policy='create-only',
    )
    calls = 0

    def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise migration.SourceWriteFreezeError('source-write freeze lease expired')

    with pytest.raises(migration.SourceWriteFreezeError, match='lease expired'):
        migration.run_verify(
            source,
            target,
            plan,
            manifest,
            checkpoint_path,
            existing_policy='create-only',
            source_read_guard=guard,
        )

    assert calls == 4
    checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
    assert checkpoint['status'] == 'failed'
    assert 'source_live_count' not in checkpoint


@pytest.mark.parametrize('existing_policy', ['create-only', 'same-hash'])
def test_policy_resumes_after_crash_between_target_write_and_checkpoint(
    tmp_path: Path,
    existing_policy: str,
) -> None:
    source, plan, manifest = _captured(tmp_path)
    target = _FakeTarget()
    target.fail_after_store_once = True
    checkpoint_path = tmp_path / 'checkpoint.json'

    with pytest.raises(RuntimeError, match='controlled interruption'):
        migration.run_apply(
            source,
            target,
            plan,
            manifest,
            checkpoint_path,
            existing_policy=existing_policy,
        )
    failed = json.loads(checkpoint_path.read_text(encoding='utf-8'))
    assert failed['status'] == 'failed'
    assert failed['next_index'] == 0
    with pytest.raises(migration.StorageMigrationError, match='every inventoried object'):
        migration.run_verify(
            source,
            target,
            plan,
            manifest,
            checkpoint_path,
            existing_policy=existing_policy,
        )

    result = migration.run_apply(
        source,
        target,
        plan,
        manifest,
        checkpoint_path,
        existing_policy=existing_policy,
    )

    assert result['status'] == 'applied'
    assert target.put_calls == 2
    assert len(target.objects) == 2


def test_create_only_refuses_a_nonempty_target_without_creating_a_checkpoint(tmp_path: Path) -> None:
    source, plan, manifest = _captured(tmp_path)
    target = _FakeTarget()
    target.objects[('omi-speech-profiles', 'unrelated')] = _TargetValue(b'x', {}, None)
    checkpoint_path = tmp_path / 'checkpoint.json'

    with pytest.raises(migration.TargetConflictError, match='empty target scopes'):
        migration.run_apply(
            source,
            target,
            plan,
            manifest,
            checkpoint_path,
            existing_policy='create-only',
        )

    assert not checkpoint_path.exists()
    assert target.put_calls == 0


def test_same_hash_policy_rejects_corrupt_existing_target_bytes(tmp_path: Path) -> None:
    source, plan, manifest = _captured(tmp_path)
    target = _FakeTarget()
    record = manifest.records[0]
    target.objects[(record.target_bucket, record.target_name)] = _TargetValue(
        content=b'x' * record.size,
        metadata=migration._expected_target_metadata(record, plan.sha256),
        content_type=record.content_type,
    )

    with pytest.raises(migration.StorageReconciliationError, match='bytes do not match'):
        migration.run_apply(
            source,
            target,
            plan,
            manifest,
            tmp_path / 'checkpoint.json',
            existing_policy='same-hash',
        )

    assert target.put_calls == 0


def test_final_reconciliation_rejects_source_generation_drift(tmp_path: Path) -> None:
    source, plan, manifest = _captured(tmp_path)
    target = _FakeTarget()

    def mutate_after_first_copy() -> None:
        if target.put_calls == 1:
            source.add(
                'production-speech-profiles',
                'tenant-a/speech_profile.wav',
                b'changed-source',
                generation='99',
                content_type='audio/wav',
            )

    target.after_put = mutate_after_first_copy
    checkpoint = tmp_path / 'checkpoint.json'

    migration.run_apply(
        source,
        target,
        plan,
        manifest,
        checkpoint,
        existing_policy='create-only',
    )

    with pytest.raises(migration.StorageReconciliationError, match='count or content reconciliation'):
        migration.run_verify(
            source,
            target,
            plan,
            manifest,
            checkpoint,
            existing_policy='create-only',
        )

    assert json.loads(checkpoint.read_text(encoding='utf-8'))['status'] == 'failed'


def test_final_reconciliation_rejects_an_extra_target_object(tmp_path: Path) -> None:
    source, plan, manifest = _captured(tmp_path)
    target = _FakeTarget()
    target.objects[('omi-speech-profiles', 'extra/object.bin')] = _TargetValue(b'extra', {}, None)

    checkpoint = tmp_path / 'checkpoint.json'
    migration.run_apply(
        source,
        target,
        plan,
        manifest,
        checkpoint,
        existing_policy='same-hash',
    )

    with pytest.raises(migration.StorageReconciliationError, match='count/key reconciliation'):
        migration.run_verify(
            source,
            target,
            plan,
            manifest,
            checkpoint,
            existing_policy='same-hash',
        )


def test_manifest_tampering_is_rejected_before_copy(tmp_path: Path) -> None:
    _, plan, manifest = _captured(tmp_path)
    with manifest.path.open('a', encoding='utf-8') as handle:
        handle.write('{}\n')

    with pytest.raises(migration.StorageMigrationError, match='invalid object record'):
        migration.load_manifest(manifest.path, plan)


def test_prefix_scope_preserves_relative_path_and_never_reads_another_tenant(tmp_path: Path) -> None:
    plan = _write_plan(
        tmp_path / 'plan.json',
        scopes=[
            {
                'id': 'tenant-a',
                'source_bucket': 'source-bucket',
                'source_prefix': 'tenants/a/',
                'target_bucket': 'target-bucket',
                'target_prefix': 'accounts/a/',
            }
        ],
    )
    source = _FakeSource()
    source.add('source-bucket', 'tenants/a/audio/one.wav', b'audio')
    source.add('source-bucket', 'tenants/b/audio/two.wav', b'other')

    manifest = migration.capture_inventory(source, plan, tmp_path / 'inventory.jsonl')

    assert [record.source_name for record in manifest.records] == ['tenants/a/audio/one.wav']
    assert [record.target_name for record in manifest.records] == ['accounts/a/audio/one.wav']


def test_inventory_rejects_unsafe_object_names_and_reserved_metadata(tmp_path: Path) -> None:
    plan = _write_plan(tmp_path / 'plan.json')
    source = _FakeSource()
    source.add('production-speech-profiles', 'tenant-a/../escape', b'x')

    with pytest.raises(migration.StorageMigrationError, match='object name is unsafe'):
        migration.capture_inventory(source, plan, tmp_path / 'unsafe.jsonl')

    source.objects.clear()
    source.add(
        'production-speech-profiles',
        'tenant-a/safe',
        b'x',
        metadata={'omi-migration-source-sha256': 'attacker-value'},
    )
    with pytest.raises(migration.StorageMigrationError, match='unsupported key'):
        migration.capture_inventory(source, plan, tmp_path / 'reserved.jsonl')

    source.objects.clear()
    source.objects[('production-speech-profiles', 'tenant-a/too-large')] = _SourceValue(
        content=b'',
        generation='1',
        metadata={},
        content_type=None,
    )
    original_list = source.list_objects

    def list_oversized(scope: migration.Scope) -> Iterable[migration.SourceDescriptor]:
        for descriptor in original_list(scope):
            yield migration.SourceDescriptor(
                bucket=descriptor.bucket,
                name=descriptor.name,
                generation=descriptor.generation,
                size=migration._MAX_OBJECT_BYTES + 1,
                metadata=descriptor.metadata,
                content_type=descriptor.content_type,
            )

    source.list_objects = list_oversized  # type: ignore[method-assign]
    with pytest.raises(migration.StorageMigrationError, match='size is invalid'):
        migration.capture_inventory(source, plan, tmp_path / 'oversized.jsonl')


def test_minio_multipart_stream_uses_create_only_completion_and_bounded_parts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, plan, manifest = _captured(tmp_path)
    record = manifest.records[0]
    calls: dict[str, object] = {'uploaded': [], 'aborted': False}

    class _S3:
        def create_multipart_upload(self, **kwargs):
            calls['create'] = kwargs
            return {'UploadId': 'upload-1'}

        def upload_part(self, **kwargs):
            uploaded = calls['uploaded']
            assert isinstance(uploaded, list)
            uploaded.append(kwargs)
            return {'ETag': f'etag-{kwargs["PartNumber"]}'}

        def complete_multipart_upload(self, **kwargs):
            calls['complete'] = kwargs

        def abort_multipart_upload(self, **kwargs):
            calls['aborted'] = True

    target = object.__new__(migration.MinioTarget)
    target._authority = 'http://minio:9000'
    target._client = _S3()
    monkeypatch.setattr(migration, '_SINGLE_PUT_MAX_BYTES', 4)
    monkeypatch.setattr(migration, '_MIN_MULTIPART_PART_BYTES', 5)

    target.put_object_create_only(record, io.BytesIO(b'wave-bytes-a'), plan_sha256=plan.sha256)

    uploaded = calls['uploaded']
    assert isinstance(uploaded, list)
    assert [part['Body'] for part in uploaded] == [b'wave-', b'bytes', b'-a']
    complete = calls['complete']
    assert isinstance(complete, dict)
    assert complete['IfNoneMatch'] == '*'
    assert calls['aborted'] is False
