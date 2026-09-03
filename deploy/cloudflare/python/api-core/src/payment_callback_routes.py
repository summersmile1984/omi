"""Public payment result pages used by native checkout web views."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_PAGE_STYLE = (
    "font-family: sans-serif; display: flex; align-items: center; "
    "justify-content: center; height: 100vh; margin: 0; flex-direction: column;"
)


def _payment_page(title: str, heading: str, message: str) -> HTMLResponse:
    return HTMLResponse(
        content=(
            "<html>"
            f"<head><title>{title}</title></head>"
            f'<body style="{_PAGE_STYLE}">'
            f"<h1>{heading}</h1>"
            f"<p>{message}</p>"
            "</body>"
            "</html>"
        )
    )


@router.get("/v1/payments/success", response_class=HTMLResponse)
async def stripe_success(session_id: str):
    # Stripe subscription state is authoritative through its webhook. Native
    # clients only use this URL as the terminal checkout navigation signal.
    del session_id
    return _payment_page(
        "Success",
        "Payment Successful!",
        "Your subscription is now active. You can close this window and return to the app.",
    )


@router.get("/v1/payments/cancel", response_class=HTMLResponse)
async def stripe_cancel():
    return _payment_page(
        "Cancelled",
        "Payment Cancelled",
        "Your payment process was cancelled. You can return to the app.",
    )


@router.get("/v1/payments/portal-return", response_class=HTMLResponse)
async def portal_return():
    return _payment_page(
        "Portal Complete",
        "Settings Updated",
        "Your payment settings have been updated. You can close this window and return to the app.",
    )
