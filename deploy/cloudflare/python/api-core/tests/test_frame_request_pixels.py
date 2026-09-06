"""Real HTTP/multipart/all-D1 migrations; Images and R2 IO are controlled."""

from io import BytesIO
from types import SimpleNamespace
from tempfile import SpooledTemporaryFile
from uuid import uuid4
import json

from PIL import Image, PngImagePlugin
import pytest

from test_frame_request_metadata import target, Query, BASE

from frame_request_image import _canonicalize_frame_image
from frame_image_metadata import jpeg_without_metadata
from test_frame_image_transform import native_images


class Stream:
    def __init__(self, data):
        self.data, self.used, self.cancelled, self.released = data, False, False, False

    def getReader(self):
        return self

    async def read(self):
        if self.used:
            return SimpleNamespace(done=True)
        self.used = True
        return SimpleNamespace(done=False, value=self.data)

    async def cancel(self):
        self.cancelled = True

    def releaseLock(self):
        self.released = True


class Object:
    def __init__(self, data):
        self.body = Stream(data)

    async def arrayBuffer(self):
        return self.body.data


class Upload:
    def __init__(self, bucket, key):
        self.bucket, self.key, self.uploadId = bucket, key, uuid4().hex
        self.data = b''
        self.aborted = False

    async def uploadPart(self, number, data):
        if self.aborted:
            raise RuntimeError('NoSuchUpload (10024)')
        self.data = bytes(data)
        if self.bucket.on_part:
            callback, self.bucket.on_part = self.bucket.on_part, None
            await callback()
        return SimpleNamespace(etag='fixture-etag', partNumber=number)

    async def complete(self, parts):
        if self.bucket.on_complete:
            callback, self.bucket.on_complete = self.bucket.on_complete, None
            await callback()
        if self.aborted:
            raise RuntimeError('NoSuchUpload (10024)')
        self.bucket.objects[self.key] = self.data

    async def abort(self):
        self.aborted = True


class Bucket:
    def __init__(self):
        self.objects, self.uploads = {}, {}
        self.on_part, self.on_complete, self.on_get = None, None, None

    async def createMultipartUpload(self, key, options):
        upload = Upload(self, key)
        self.uploads[upload.uploadId] = upload
        return upload

    async def get(self, key):
        data = self.objects.get(key)
        result = Object(data) if data is not None else None
        if self.on_get:
            callback, self.on_get = self.on_get, None
            await callback()
        return result


@pytest.fixture
def pixels(target, native_images):
    target.env.IMAGES = native_images
    target.env.FRAME_REQUESTS_TEMPORARY = Bucket()
    target.env.FRAME_REQUESTS = Bucket()
    target.db.connection.execute("INSERT INTO cf_conversations(uid,id,created_at) VALUES ('owner','meeting',1)")
    from conversation_routes import router

    target.app.include_router(router)
    return target


@pytest.fixture
def png():
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('private', 'do not store metadata')
    output = BytesIO()
    Image.new('RGBA', (1920, 1080), (36, 64, 90, 180)).save(output, format='PNG', pnginfo=metadata)
    return output.getvalue()


def claimed(f, conversation=None):
    response = f.call('POST', body={'device_id': 'desktop', 'dedupe_key': uuid4().hex, 'conversation_id': conversation})
    assert response.status_code == 200, response.text
    row = response.json()['request']
    result = f.call('POST', f"{BASE}/{row['request_id']}/state", body={'device_id': 'desktop', 'state': 'claimed'})
    assert result.status_code == 200, result.text
    return row


def upload(f, row, data, mime='image/png', device='desktop'):
    return f.call(
        'POST',
        f"{BASE}/{row['request_id']}/upload?device_id={device}&account_generation=0",
        files={'file': ('fixture.png', data, mime)},
    )


def promote(f, row):
    return f.call(
        'POST',
        f"{BASE}/{row['request_id']}/promote",
        body={'device_id': 'desktop', 'account_generation': 0, 'conversation_id': 'meeting'},
    )


def test_upload_canonical_bytes_private_read_and_owner_isolation(pixels, png):
    row = claimed(pixels)
    result = upload(pixels, row, png)
    assert result.status_code == 200, result.text
    result = result.json()['request']
    assert result['state'] == 'uploaded' and result['storage_id'].startswith('temporary-')
    expected = jpeg_without_metadata(_canonicalize_frame_image(png))
    assert list(pixels.env.FRAME_REQUESTS_TEMPORARY.objects.values()) == [expected]
    assert b'do not store metadata' not in expected
    path = f"{BASE}/temporary/{row['request_id']}/image"
    response = pixels.call('GET', path)
    assert response.status_code == 200 and response.content == expected
    assert response.headers['content-type'] == 'image/jpeg' and response.headers['cache-control'] == 'no-store'
    assert pixels.call('GET', path, uid='other').status_code == 404


def test_promotion_is_atomic_and_uses_conversation_lifetime_read(pixels, png):
    row = claimed(pixels, 'meeting')
    assert upload(pixels, row, png).status_code == 200
    assert pixels.call('GET', f"{BASE}/temporary/{row['request_id']}/image").status_code == 404
    result = promote(pixels, row)
    assert result.status_code == 200, result.text
    attached = result.json()['request']
    assert attached['state'] == 'attached' and attached['expires_at'] == attached['created_at']
    assert attached['cleanup_state'] == 'permanent' and attached['storage_id'] == 'permanent-' + row['request_id'][6:]
    assert promote(pixels, row).json() == result.json()
    photos = pixels.call('GET', '/v1/conversations/meeting/photos').json()
    assert len(photos) == 1 and photos[0]['storage_id'] == attached['storage_id'] and photos[0]['base64'] == ''
    markers = pixels.db.connection.execute(
        "SELECT has_content,has_photos FROM cf_conversations WHERE uid='owner' AND id='meeting'"
    ).fetchone()
    assert tuple(markers) == (1, 1)
    path = f"/v1/conversations/meeting/photos/{row['request_id']}/image"
    assert pixels.call('GET', path).content == jpeg_without_metadata(_canonicalize_frame_image(png))
    # Permanent evidence remains owned by its conversation when JIT is stopped.
    pixels.db.connection.execute("UPDATE cf_jit_flags SET kill_switch=1 WHERE uid=''")
    assert pixels.call('GET', path).status_code == 200
    assert pixels.call('GET', path, uid='other').status_code == 404
    pixels.db.connection.execute("DELETE FROM cf_conversations WHERE uid='owner' AND id='meeting'")
    assert pixels.call('GET', path).status_code == 404
    assert {r[0] for r in pixels.db.connection.execute('SELECT phase FROM cf_frame_objects')} == {'cleanup'}


def test_two_concurrent_promotions_reference_one_permanent_object(pixels, png):
    row = claimed(pixels, 'meeting')
    assert upload(pixels, row, png).status_code == 200
    winners = []

    async def other():
        winners.append(
            await pixels.async_call(
                'POST',
                f"{BASE}/{row['request_id']}/promote",
                body={'device_id': 'desktop', 'account_generation': 0, 'conversation_id': 'meeting'},
            )
        )

    pixels.env.FRAME_REQUESTS.on_complete = other
    response = promote(pixels, row)
    assert response.status_code == 200, response.text
    assert winners[0].status_code == 200, winners[0].text
    assert response.json() == winners[0].json()
    rows = pixels.db.connection.execute("SELECT tier,phase FROM cf_frame_objects").fetchall()
    assert sum(r['tier'] == 'permanent' and r['phase'] == 'live' for r in rows) == 1
    assert sum(r['phase'] == 'cleanup' for r in rows) == 2
    assert len(pixels.call('GET', '/v1/conversations/meeting/photos').json()) == 1


def test_ambiguous_publication_preserves_committed_image(pixels, png, monkeypatch):
    row = claimed(pixels)
    original = Query.first
    lost = False

    async def first(statement):
        nonlocal lost
        result = await original(statement)
        if "SET phase='live'" in statement.sql and not lost:
            lost = True
            raise RuntimeError('response lost after commit')
        return result

    monkeypatch.setattr(Query, 'first', first)
    result = upload(pixels, row, png)
    assert result.status_code == 200, result.text
    assert pixels.db.connection.execute('SELECT phase FROM cf_frame_objects').fetchone()[0] == 'live'
    assert pixels.call('GET', f"{BASE}/temporary/{row['request_id']}/image").content == jpeg_without_metadata(
        _canonicalize_frame_image(png)
    )


@pytest.mark.parametrize('boundary', ['canonicalization', 'part', 'release'])
def test_revocation_during_processing_or_fetch_releases_no_image(pixels, png, monkeypatch, boundary):
    row = claimed(pixels)

    def kill():
        pixels.db.connection.execute("UPDATE cf_jit_flags SET kill_switch=1 WHERE uid=''")

    async def async_kill():
        kill()

    if boundary == 'canonicalization':
        pixels.env.IMAGES.after_output = async_kill
    elif boundary == 'part':
        pixels.env.FRAME_REQUESTS_TEMPORARY.on_part = async_kill
    response = upload(pixels, row, png)
    if boundary == 'release':
        assert response.status_code == 200, response.text
        pixels.env.FRAME_REQUESTS_TEMPORARY.on_get = async_kill
        response = pixels.call('GET', f"{BASE}/temporary/{row['request_id']}/image")
        assert response.status_code == 404
    else:
        assert response.status_code in {404, 409}
        assert not pixels.env.FRAME_REQUESTS_TEMPORARY.objects
    if boundary == 'canonicalization':
        assert not pixels.env.FRAME_REQUESTS_TEMPORARY.uploads
    if boundary == 'part':
        stored = pixels.db.connection.execute('SELECT upload_id,phase FROM cf_frame_objects').fetchone()
        assert stored['upload_id'] and stored['phase'] == 'cleanup'


def test_bad_uploads_and_device_mismatch_never_publish(pixels, png):
    row = claimed(pixels)
    assert upload(pixels, row, b'corrupt').status_code == 415
    assert upload(pixels, row, png, mime='application/octet-stream').status_code == 415
    assert upload(pixels, row, b'x' * (10 * 1024 * 1024 + 1)).status_code == 413
    assert upload(pixels, row, png, device='other').status_code == 403
    assert not pixels.env.FRAME_REQUESTS_TEMPORARY.objects


def test_native_transform_outage_leaves_claim_retryable_and_no_r2_write(pixels, png):
    row = claimed(pixels)
    pixels.env.IMAGES.failure = RuntimeError('images unavailable')
    assert upload(pixels, row, png).status_code == 503
    assert not pixels.env.FRAME_REQUESTS_TEMPORARY.uploads
    assert pixels.db.connection.execute('SELECT count(*) FROM cf_frame_objects').fetchone()[0] == 0
    pixels.env.IMAGES.failure = None
    assert upload(pixels, row, png).status_code == 200


def test_ten_mib_upload_uses_no_temporary_file_and_preserves_wire_validation(pixels, png, monkeypatch):
    def no_disk(*args, **kwargs):
        raise OSError(8, 'Bad file descriptor')

    monkeypatch.setattr(SpooledTemporaryFile, 'rollover', no_disk)
    row = claimed(pixels)
    assert upload(pixels, row, png.ljust(10 * 1024 * 1024, b'\0')).status_code == 200
    other = claimed(pixels)
    assert upload(pixels, other, png.ljust(10 * 1024 * 1024 + 1, b'\0')).status_code == 413
    path = f"{BASE}/{other['request_id']}/upload?device_id=desktop&account_generation=0"
    assert pixels.call('POST', path, body={}).status_code == 422
    assert not any(upload.aborted for upload in pixels.env.FRAME_REQUESTS_TEMPORARY.uploads.values())


def test_photo_identity_conflict_rolls_back_both_object_and_frame(pixels, png):
    row = claimed(pixels, 'meeting')
    assert upload(pixels, row, png).status_code == 200
    pixels.db.connection.execute(
        "UPDATE cf_conversations SET photos_json=? WHERE uid='owner' AND id='meeting'",
        (json.dumps([{'id': row['request_id'], 'storage_id': 'unrelated', 'base64': ''}]),),
    )
    result = promote(pixels, row)
    assert result.status_code == 409, result.text
    state = pixels.call('GET', BASE + '/status/' + row['request_id']).json()['request']['state']
    assert state == 'uploaded'
    assert (
        pixels.db.connection.execute(
            "SELECT count(*) FROM cf_frame_objects WHERE tier='permanent' AND phase='live'"
        ).fetchone()[0]
        == 0
    )
