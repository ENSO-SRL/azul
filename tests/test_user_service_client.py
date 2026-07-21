"""
Tests para app.services.user_service_client.

Valida:
- 200 -> True
- 401 -> False (sin lanzar)
- 422 -> False (sin lanzar)
- 502 -> False (sin lanzar)
- Timeout -> False (sin lanzar)
- Error de red -> False (sin lanzar)
- Modo dev (sin env vars) -> True sin llamar HTTP
- PAYMENT_EMAIL_ENABLED=0 -> True sin llamar HTTP

Ejecutar: pytest tests/test_user_service_client.py -v
"""

from __future__ import annotations

import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock

from app.services import user_service_client as usc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE = "https://user-service.example.com"
_KEY  = "test-key-abc123"


def _env(monkeypatch):
    """Fija las env vars de config."""
    monkeypatch.setenv("ATLAS_API_BASE_URL", _BASE)
    monkeypatch.setenv("USER_SERVICE_KEY", _KEY)
    monkeypatch.setenv("PAYMENT_EMAIL_ENABLED", "1")


def _mock_response(status_code: int, text: str = "ok") -> httpx.Response:
    return httpx.Response(status_code=status_code, text=text)


# ---------------------------------------------------------------------------
# Respuestas HTTP
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_200_returns_true(monkeypatch):
    """200 del user service -> True."""
    _env(monkeypatch)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_mock_response(200))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.user_service_client.httpx.AsyncClient", return_value=mock_client):
        result = await usc.enviar_correo_pago(
            to_email="cliente@test.com",
            success=True,
            invoice_number="INV-001",
            total=1500.00,
        )

    assert result is True
    mock_client.post.assert_awaited_once()
    call_kwargs = mock_client.post.call_args
    assert call_kwargs.kwargs["headers"]["x-service-key"] == _KEY
    assert call_kwargs.kwargs["json"]["success"] is True
    assert call_kwargs.kwargs["json"]["invoice_number"] == "INV-001"
    assert call_kwargs.kwargs["json"]["total"] == 1500.00


@pytest.mark.asyncio
async def test_401_returns_false_no_raise(monkeypatch):
    """401 -> False, no lanza."""
    _env(monkeypatch)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_mock_response(401, "Unauthorized"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.user_service_client.httpx.AsyncClient", return_value=mock_client):
        result = await usc.enviar_correo_pago(to_email="x@x.com", success=False)

    assert result is False


@pytest.mark.asyncio
async def test_422_returns_false_no_raise(monkeypatch):
    """422 -> False, no lanza."""
    _env(monkeypatch)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_mock_response(422, "Unprocessable"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.user_service_client.httpx.AsyncClient", return_value=mock_client):
        result = await usc.enviar_correo_pago(to_email="x@x.com", success=True)

    assert result is False


@pytest.mark.asyncio
async def test_502_returns_false_no_raise(monkeypatch):
    """502 -> False, no lanza."""
    _env(monkeypatch)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_mock_response(502, "Bad Gateway"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.user_service_client.httpx.AsyncClient", return_value=mock_client):
        result = await usc.enviar_correo_pago(to_email="x@x.com", success=True)

    assert result is False


@pytest.mark.asyncio
async def test_timeout_returns_false_no_raise(monkeypatch):
    """Timeout -> False, no lanza."""
    _env(monkeypatch)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.user_service_client.httpx.AsyncClient", return_value=mock_client):
        result = await usc.enviar_correo_pago(to_email="x@x.com", success=False)

    assert result is False


@pytest.mark.asyncio
async def test_network_error_returns_false_no_raise(monkeypatch):
    """Error de red (RequestError) -> False, no lanza."""
    _env(monkeypatch)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(
        side_effect=httpx.RequestError("connection refused")
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.user_service_client.httpx.AsyncClient", return_value=mock_client):
        result = await usc.enviar_correo_pago(to_email="x@x.com", success=False)

    assert result is False


# ---------------------------------------------------------------------------
# Modo dev / flag
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dev_mode_no_base_url_returns_true_no_http(monkeypatch):
    """Sin ATLAS_API_BASE_URL configurada -> True sin llamar HTTP."""
    monkeypatch.delenv("ATLAS_API_BASE_URL", raising=False)
    monkeypatch.delenv("USER_SERVICE_KEY", raising=False)
    monkeypatch.setenv("PAYMENT_EMAIL_ENABLED", "1")

    with patch("app.services.user_service_client.httpx.AsyncClient") as mock_cls:
        result = await usc.enviar_correo_pago(to_email="dev@local.com", success=True)

    assert result is True
    mock_cls.assert_not_called()


@pytest.mark.asyncio
async def test_payment_email_disabled_returns_true_no_http(monkeypatch):
    """PAYMENT_EMAIL_ENABLED=0 -> True sin llamar HTTP."""
    _env(monkeypatch)
    monkeypatch.setenv("PAYMENT_EMAIL_ENABLED", "0")

    with patch("app.services.user_service_client.httpx.AsyncClient") as mock_cls:
        result = await usc.enviar_correo_pago(to_email="x@x.com", success=True)

    assert result is True
    mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failure_payload_has_failure_reason(monkeypatch):
    """success=False con failure_reason -> el campo aparece en el payload."""
    _env(monkeypatch)

    captured = {}

    async def fake_post(url, *, json, headers, **kw):
        captured["json"] = json
        return _mock_response(200)

    mock_client = AsyncMock()
    mock_client.post = fake_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.user_service_client.httpx.AsyncClient", return_value=mock_client):
        await usc.enviar_correo_pago(
            to_email="x@x.com",
            success=False,
            failure_reason="Fondos insuficientes",
        )

    assert captured["json"]["success"] is False
    assert captured["json"]["failure_reason"] == "Fondos insuficientes"
    assert "invoice_number" not in captured["json"]


@pytest.mark.asyncio
async def test_success_payload_omits_none_fields(monkeypatch):
    """Campos None/omitidos no aparecen en el payload JSON."""
    _env(monkeypatch)

    captured = {}

    async def fake_post(url, *, json, headers, **kw):
        captured["json"] = json
        return _mock_response(200)

    mock_client = AsyncMock()
    mock_client.post = fake_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.user_service_client.httpx.AsyncClient", return_value=mock_client):
        await usc.enviar_correo_pago(
            to_email="c@c.com",
            success=True,
            invoice_number="INV-999",
            total=500.0,
        )

    j = captured["json"]
    assert "name" not in j
    assert "failure_reason" not in j
    assert "payment_method" not in j
    assert j["invoice_number"] == "INV-999"
    assert j["total"] == 500.0
