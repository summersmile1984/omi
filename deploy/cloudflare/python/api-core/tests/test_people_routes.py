import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from people_routes import (  # noqa: E402
    delete_person,
    delete_person_speech_sample,
    get_or_create_person,
    get_person,
    list_people,
    rename_person,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration = Path(__file__).parents[3] / "migrations/app/0017_people.sql"
        self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeStatement:
    def __init__(self, connection, sql):
        self.connection = connection
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def first(self):
        row = self.connection.execute(self.sql, self.args).fetchone()
        return dict(row) if row is not None else None

    async def all(self):
        rows = self.connection.execute(self.sql, self.args).fetchall()
        return {"results": [dict(row) for row in rows]}

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class FakeRequest:
    def __init__(self, env, headers, body=None, query=None):
        self.scope = {"env": env}
        self.headers = headers
        self.body = body
        self.query_params = query or {}
        self.url = "https://staging.example.test/v1/users/people"

    async def json(self):
        return self.body


class FakeBucket:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.deleted = []
        self.fail_delete = False

    async def head(self, key):
        return {"key": key, "size": len(self.objects[key])} if key in self.objects else None

    async def get(self, key, options=None):
        del options
        return self.objects.get(key)

    async def put(self, key, value, **options):
        del options
        self.objects[key] = value

    async def list(self, options):
        prefix = options.get("prefix", "")
        return {
            "objects": [{"key": key} for key in sorted(self.objects) if key.startswith(prefix)],
            "truncated": False,
        }

    async def delete(self, key):
        if self.fail_delete:
            raise RuntimeError("R2 unavailable")
        self.deleted.append(key)
        self.objects.pop(key, None)


def _env(secret: str, bucket=None):
    return type(
        "Env",
        (),
        {
            "APP_DB": FakeDb(),
            "INTERNAL_ASSERTION_SECRET": secret,
            "SPEECH_PROFILES": bucket or FakeBucket(),
        },
    )()


def _set_samples(env, uid, person_id, samples, transcripts):
    env.APP_DB.connection.execute(
        "UPDATE cf_people SET speech_samples_json = ?, speech_sample_transcripts_json = ? WHERE uid = ? AND id = ?",
        (json.dumps(samples), json.dumps(transcripts) if transcripts is not None else None, uid, person_id),
    )
    env.APP_DB.connection.commit()


def _signed_key(url: str) -> str:
    token = parse_qs(urlsplit(url).query)["token"][0]
    encoded = token.split(".", 1)[0]
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode())
    return payload["k"]


def signed_headers(secret: str, uid: str = "people-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "people-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def test_people_metadata_is_uid_scoped_and_name_idempotent():
    secret = "people-secret"
    env = _env(secret)
    headers = signed_headers(secret)

    invalid = asyncio.run(get_or_create_person(FakeRequest(env, headers, {"name": "A"})))
    assert invalid.status_code == 400

    created = asyncio.run(get_or_create_person(FakeRequest(env, headers, {"name": "Alice"})))
    assert created["id"]
    assert created["name"] == "Alice"
    assert created["speech_samples"] == []
    assert created["speech_samples_version"] == 3

    retry = asyncio.run(get_or_create_person(FakeRequest(env, headers, {"name": "Alice"})))
    assert retry["id"] == created["id"]

    listed = asyncio.run(list_people(FakeRequest(env, headers)))
    assert listed == [created]
    without_samples = asyncio.run(list_people(FakeRequest(env, headers, query={"include_speech_samples": "false"})))
    assert without_samples[0]["speech_samples"] == []

    renamed = asyncio.run(rename_person(FakeRequest(env, headers, query={"value": "Alice Chen"}), created["id"]))
    assert renamed == {"status": "ok"}
    fetched = asyncio.run(get_person(FakeRequest(env, headers), created["id"]))
    assert fetched["name"] == "Alice Chen"

    other = asyncio.run(get_person(FakeRequest(env, signed_headers(secret, "other-user")), created["id"]))
    assert other.status_code == 404

    deleted = asyncio.run(delete_person(FakeRequest(env, headers), created["id"]))
    assert deleted.status_code == 204
    missing = asyncio.run(get_person(FakeRequest(env, headers), created["id"]))
    assert missing.status_code == 404


def test_people_routes_reject_invalid_boolean_and_duplicate_rename():
    secret = "people-secret"
    env = _env(secret)
    headers = signed_headers(secret)
    first = asyncio.run(get_or_create_person(FakeRequest(env, headers, {"name": "Alice"})))
    second = asyncio.run(get_or_create_person(FakeRequest(env, headers, {"name": "Bob"})))

    invalid_filter = asyncio.run(list_people(FakeRequest(env, headers, query={"include_speech_samples": "maybe"})))
    assert invalid_filter.status_code == 400

    duplicate = asyncio.run(rename_person(FakeRequest(env, headers, query={"value": "Bob"}), first["id"]))
    assert duplicate.status_code == 409

    not_found = asyncio.run(rename_person(FakeRequest(env, headers, query={"value": "Carol"}), "missing-person"))
    assert not_found.status_code == 404
    assert second["name"] == "Bob"


def test_people_speech_samples_use_signed_urls_and_delete_the_aligned_r2_sample():
    secret = "people-secret-at-least-16"
    uid = "people-user"
    first_key = f"{uid}/people_profiles/person-placeholder/first.wav"
    second_key = f"{uid}/people_profiles/person-placeholder/second.wav"
    bucket = FakeBucket({first_key: b"first", second_key: b"second"})
    env = _env(secret, bucket)
    headers = signed_headers(secret, uid)
    person = asyncio.run(get_or_create_person(FakeRequest(env, headers, {"name": "Alice"})))
    first_key = first_key.replace("person-placeholder", person["id"])
    second_key = second_key.replace("person-placeholder", person["id"])
    bucket.objects = {first_key: b"first", second_key: b"second"}
    _set_samples(env, uid, person["id"], [first_key, second_key], ["first transcript", "second transcript"])

    listed = asyncio.run(list_people(FakeRequest(env, headers)))
    assert [_signed_key(url) for url in listed[0]["speech_samples"]] == [first_key, second_key]
    fetched = asyncio.run(
        get_person(
            FakeRequest(env, headers, query={"include_speech_samples": "true"}),
            person["id"],
        )
    )
    assert [_signed_key(url) for url in fetched["speech_samples"]] == [first_key, second_key]

    deleted = asyncio.run(delete_person_speech_sample(FakeRequest(env, headers), person["id"], 0))
    assert deleted == {"status": "ok"}
    assert bucket.deleted == [first_key]
    row = asyncio.run(get_person(FakeRequest(env, headers, query={"include_speech_samples": "true"}), person["id"]))
    assert [_signed_key(url) for url in row["speech_samples"]] == [second_key]
    assert row["speech_sample_transcripts"] == ["second transcript"]

    missing = asyncio.run(delete_person_speech_sample(FakeRequest(env, headers), person["id"], 8))
    assert missing.status_code == 404


def test_people_speech_sample_failures_preserve_metadata_and_never_sign_cross_user_keys():
    secret = "people-secret-at-least-16"
    uid = "people-user"
    bucket = FakeBucket()
    env = _env(secret, bucket)
    headers = signed_headers(secret, uid)
    person = asyncio.run(get_or_create_person(FakeRequest(env, headers, {"name": "Alice"})))
    key = f"{uid}/people_profiles/{person['id']}/sample.wav"
    bucket.objects[key] = b"sample"
    _set_samples(env, uid, person["id"], [key], ["sample transcript"])
    bucket.fail_delete = True

    unavailable = asyncio.run(delete_person_speech_sample(FakeRequest(env, headers), person["id"], 0))
    assert unavailable.status_code == 503
    retained = asyncio.run(
        get_person(FakeRequest(env, headers, query={"include_speech_samples": "false"}), person["id"])
    )
    assert retained["id"] == person["id"]

    _set_samples(env, uid, person["id"], [f"other-user/people_profiles/{person['id']}/sample.wav"], [])
    corrupt = asyncio.run(list_people(FakeRequest(env, headers)))
    assert corrupt.status_code == 503


def test_deleting_person_purges_the_r2_prefix_before_removing_d1_metadata():
    secret = "people-secret-at-least-16"
    uid = "people-user"
    bucket = FakeBucket()
    env = _env(secret, bucket)
    headers = signed_headers(secret, uid)
    person = asyncio.run(get_or_create_person(FakeRequest(env, headers, {"name": "Alice"})))
    keys = [
        f"{uid}/people_profiles/{person['id']}/first.wav",
        f"{uid}/people_profiles/{person['id']}/second.wav",
    ]
    bucket.objects = {key: b"sample" for key in keys}
    _set_samples(env, uid, person["id"], keys, ["first", "second"])
    bucket.fail_delete = True

    unavailable = asyncio.run(delete_person(FakeRequest(env, headers), person["id"]))
    assert unavailable.status_code == 503
    retained = asyncio.run(get_person(FakeRequest(env, headers), person["id"]))
    assert retained["id"] == person["id"]

    bucket.fail_delete = False
    deleted = asyncio.run(delete_person(FakeRequest(env, headers), person["id"]))
    assert deleted.status_code == 204
    assert bucket.objects == {}
    assert bucket.deleted == keys
    missing = asyncio.run(get_person(FakeRequest(env, headers), person["id"]))
    assert missing.status_code == 404
