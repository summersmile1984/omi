import pytest

from utils.product_telemetry import emit_product_event, set_product_telemetry_client_for_tests


class _FakePosthog:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.events = []

    def capture(self, **event):
        if self.fail:
            raise RuntimeError('posthog unavailable')
        self.events.append(event)


@pytest.fixture(autouse=True)
def _reset_client():
    yield
    set_product_telemetry_client_for_tests(None)


def test_product_event_uses_uid_only_as_distinct_id_and_drops_null_properties(monkeypatch):
    fake = _FakePosthog()
    set_product_telemetry_client_for_tests(fake)
    monkeypatch.setenv('OMI_ENV_STAGE', 'dev')

    emit_product_event(
        uid='user-1',
        event='Transcript Started',
        properties={'recording_id': 'recording-1', 'conversation_id': None},
    )

    assert fake.events == [
        {
            'distinct_id': 'user-1',
            'event': 'Transcript Started',
            'properties': {'recording_id': 'recording-1', 'environment': 'dev'},
        }
    ]


def test_product_event_is_fail_open():
    set_product_telemetry_client_for_tests(_FakePosthog(fail=True))

    emit_product_event(uid='user-1', event='Transcript Failed', properties={})


def test_self_hosted_profile_ignores_ambient_posthog_configuration(monkeypatch):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.setenv('POSTHOG_PROJECT_API_KEY', 'ambient-managed-key')
    monkeypatch.delenv('POSTHOG_HOST', raising=False)
    monkeypatch.setattr(
        'utils.product_telemetry.importlib.import_module',
        lambda _name: pytest.fail('self-hosted telemetry must not import PostHog'),
    )
    # The fixture reset intentionally disables the client; re-open the lazy
    # path so this exercises the profile guard rather than that test seam.
    import utils.product_telemetry as telemetry

    telemetry._posthog_client = None
    telemetry._posthog_disabled = False
    emit_product_event(uid='user-1', event='Transcript Started', properties={})
    assert telemetry._posthog_client is None
