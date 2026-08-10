import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch

from app.main import app
from app.utils.token_utils import require_user_info

@pytest.fixture
def override_auth():
    app.dependency_overrides[require_user_info] = lambda: {"sub": "user123", "email": "test@test.com"}
    yield
    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_pay_with_token_no_auth():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post("/checkout/pay-with-token", data={"card_id": "1234"})
    # Without auth it returns a 302 redirect to login due to require_user_info dependency
    assert response.status_code == 302

@pytest.mark.asyncio
@patch("app.utils.token_utils.decode_user_info_token")
async def test_pay_with_token_csrf_failure(mock_decode, override_auth):
    mock_decode.return_value = {"sub": "user123"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post("/checkout/pay-with-token", data={"card_id": "1234"})
    # CSRF fail
    assert response.status_code == 400
    assert "Error de seguridad (CSRF)" in response.text

@pytest.mark.asyncio
@patch("app.utils.token_utils.decode_user_info_token")
async def test_pay_with_token_card_not_found(mock_decode, override_auth):
    mock_decode.return_value = {"sub": "user123"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/checkout/pay-with-token",
            data={"card_id": "nonexistent_card", "csrf_token": "valid_token"},
            cookies={"checkout_csrf": "valid_token"}
        )
    # The route returns a 404 if the card is not found or invalid
    assert response.status_code == 404
    assert "Tarjeta no encontrada" in response.text
