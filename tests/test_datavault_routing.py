"""Validate the documented AZUL endpoint without making network requests."""
import json
from types import SimpleNamespace

import httpx
import pytest

from app.infrastructure import azul_gateway as gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("env", ["sandbox", "production"])
@pytest.mark.parametrize("operation", ["CREATE", "DELETE"])
async def test_datavault_operations_use_dedicated_endpoint(monkeypatch, env, operation):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.query == b"ProcessDatavault"
        payload = json.loads(request.content)
        assert payload["TrxType"] == operation
        assert "Amount" not in payload
        return httpx.Response(200, json={"ResponseCode": "ISO8583", "IsoCode": "00",
                                        "DataVaultToken": "test-token", "Brand": "VISA"})

    monkeypatch.setattr(gateway, "load_azul_config", lambda: SimpleNamespace(env=env, merchant_id="test"))
    monkeypatch.setattr(gateway.AzulPaymentGateway, "_build_client",
                        staticmethod(lambda mode: httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    adapter = gateway.AzulPaymentGateway()
    if operation == "CREATE":
        card = await adapter.create_token("228", "4242424242424242", "209912", "123")
        assert card.token == "test-token"
    else:
        await adapter.delete_token("test-token")
    assert len(requests) == 1
    assert requests[0].url.host == ("pagos.azul.com.do" if env == "production" else "pruebas.azul.com.do")


@pytest.mark.asyncio
@pytest.mark.parametrize("datavault", [False, True])
async def test_connect_failover_preserves_operation(monkeypatch, datavault):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("connection unavailable", request=request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await gateway._post_with_failover(client, {}, "production", datavault=datavault)
    assert [r.url.host for r in requests] == ["pagos.azul.com.do", "contpagos.azul.com.do"]
    assert all(r.url.query == (b"ProcessDatavault" if datavault else b"") for r in requests)
