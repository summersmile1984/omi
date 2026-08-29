import asyncio
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from payment_callback_routes import portal_return, router, stripe_cancel, stripe_success

app = FastAPI()
app.include_router(router)


def _body(response: HTMLResponse) -> str:
    assert response.status_code == 200
    assert response.media_type == "text/html"
    return response.body.decode("utf-8")


def test_checkout_success_page_preserves_native_navigation_contract():
    body = _body(asyncio.run(stripe_success("cs_test_123")))

    assert "<title>Success</title>" in body
    assert "Payment Successful!" in body
    assert "return to the app" in body


def test_checkout_success_page_requires_session_id():
    response = TestClient(app).get("/v1/payments/success")

    assert response.status_code == 422


def test_checkout_cancel_page_preserves_native_navigation_contract():
    body = _body(asyncio.run(stripe_cancel()))

    assert "<title>Cancelled</title>" in body
    assert "Payment Cancelled" in body
    assert "return to the app" in body


def test_customer_portal_return_page_preserves_native_navigation_contract():
    body = _body(asyncio.run(portal_return()))

    assert "<title>Portal Complete</title>" in body
    assert "Settings Updated" in body
    assert "return to the app" in body
