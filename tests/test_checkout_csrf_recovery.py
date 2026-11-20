"""Checkout browser regressions; only in-memory storage and gateway doubles."""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.infrastructure.database import get_db
from routers import checkout
from test_subscription_identity import storage, EMAIL


def form_token(response):
    match = re.search(r'name="csrf_token" id="csrf_token" value="([0-9a-f]{64})"', response.text)
    assert match, "Every retry form must contain a CSRF token"
    assert "no-store" in response.headers["cache-control"]
    assert "HttpOnly" in response.headers["set-cookie"]
    return match[1]


@pytest.fixture
def checkout_app(storage):
    db = storage()
    svc = SimpleNamespace(process_sale=AsyncMock(), process_hold_verify=AsyncMock())
    tokens = SimpleNamespace(
        register_card=AsyncMock(side_effect=ValueError("VALIDATION_ERROR:TrxType")),
        list_cards=AsyncMock(return_value=[]),
    )
    app = FastAPI()
    app.include_router(checkout.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[checkout._get_service] = lambda: svc
    app.dependency_overrides[checkout._get_token_svc] = lambda: tokens
    return app, svc, tokens


CARD_FORM = dict(card_number="4242424242424242", cardholder_name="Fixture",
                 cardholder_email=EMAIL, expiration="12/99", cvc="123")


@pytest.mark.asyncio
async def test_tokenization_error_can_be_retried_without_csrf_loop(checkout_app):
    app, svc, tokens = checkout_app
    with patch("app.utils.token_utils.decode_user_info_token", return_value={"sub": "228"}):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            page = await client.get("/checkout")
            assert page.status_code == 200
            token = form_token(page)
            assert client.cookies["checkout_csrf"] == token
            assert "Secure" in page.headers["set-cookie"]
            for _ in range(2):
                response = await client.post("/checkout/process", data={**CARD_FORM, "csrf_token": token})
                assert response.status_code == 503
                assert "No se pudo guardar la tarjeta" in response.text
                token = form_token(response)
                assert token == client.cookies["checkout_csrf"]
                assert CARD_FORM["card_number"] not in response.text
    assert tokens.register_card.await_count == 2
    svc.process_sale.assert_not_awaited()
    svc.process_hold_verify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/checkout/process", "/checkout/pay-with-token"])
@pytest.mark.parametrize("submitted", ["", "stale-token", "\u00f1"])
async def test_invalid_csrf_is_rejected_then_recoverable(checkout_app, route, submitted):
    app, _, tokens = checkout_app
    data = CARD_FORM if route.endswith("/process") else {"card_id": "missing"}
    with patch("app.utils.token_utils.decode_user_info_token", return_value={"sub": "228"}):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            client.cookies.set("checkout_csrf", "a" * 64, domain="test.local", path="/")
            response = await client.post(route, data={**data, "csrf_token": submitted})
            assert response.status_code == 400
            tokens.register_card.assert_not_awaited()
            tokens.list_cards.assert_not_awaited()
            token = form_token(response)
            assert token == client.cookies["checkout_csrf"]
            retry = await client.post(route, data={**data, "csrf_token": token})
            assert retry.status_code == (503 if route.endswith("/process") else 404)
            assert "Error de seguridad (CSRF)" not in retry.text
            form_token(retry)


@pytest.mark.asyncio
async def test_expired_cookie_and_multiple_tabs(checkout_app):
    app, _, _ = checkout_app
    with patch("app.utils.token_utils.decode_user_info_token", return_value={"sub": "228"}):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            first = form_token(await client.get("/checkout"))
            second = form_token(await client.get("/checkout"))
            assert first == second == client.cookies["checkout_csrf"]
            client.cookies.clear()
            response = await client.post("/checkout/process", data={**CARD_FORM, "csrf_token": first})
            assert response.status_code == 400
            fresh = form_token(response)
            assert fresh != first
            assert fresh == client.cookies["checkout_csrf"]
            retry = await client.post("/checkout/process", data={**CARD_FORM, "csrf_token": fresh})
            assert retry.status_code == 503
