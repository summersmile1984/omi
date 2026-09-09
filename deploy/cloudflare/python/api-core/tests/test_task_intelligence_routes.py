from task_intelligence_routes import _device


def test_device_scope_requires_platform_and_hash_and_rejects_cross_device():
    request = type("Request", (), {"headers": {"x-app-platform": "macos", "x-device-id-hash": "abc123"}})()
    assert _device(request, "abc123") == "macos_abc123"
    assert _device(request, "ios_other").status_code == 403
