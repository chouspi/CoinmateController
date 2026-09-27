from dataclasses import replace
from decimal import Decimal
from urllib.parse import parse_qs

from fastapi.testclient import TestClient
import httpx

from app.config import Settings
from app.main import create_app


SETTINGS = Settings(
    request_interval_seconds=0,
    coinmate_client_id="123",
    coinmate_public_key="public",
    coinmate_private_key="private",
    controller_api_token="controller-secret",
    max_market_buy_czk=Decimal("5000"),
    max_market_sell_btc=Decimal("0.5"),
    coinmate_api_url="https://coinmate.test/api",
)
IDEMPOTENCY_KEY = "7a8b9c00-e29b-41d4-a716-446655440001"
AUTH = {
    "Authorization": "Bearer controller-secret",
    "Idempotency-Key": IDEMPOTENCY_KEY,
}


def test_sell_bitcoin_returns_exact_filled_btc_amount() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sellInstant":
            form = parse_qs(request.content.decode())
            assert form["currencyPair"] == ["BTC_CZK"]
            assert form["amount"] == ["0.01234567"]
            assert form["clientOrderId"][0].isdigit()
            return httpx.Response(
                200,
                json={"error": False, "errorMessage": None, "data": 321},
            )
        assert request.url.path == "/api/orderById"
        assert parse_qs(request.content.decode())["orderId"] == ["321"]
        return httpx.Response(
            200,
            content=(
                b'{"error":false,"errorMessage":null,"data":'
                b'{"id":321,"type":"SELL","status":"FILLED",'
                b'"cumulativeAmount":0.01234567}}'
            ),
            headers={"Content-Type": "application/json"},
        )

    with TestClient(create_app(SETTINGS, httpx.MockTransport(handler))) as client:
        response = client.post(
            "/sell_bitcoin",
            headers=AUTH,
            json={"amount": 0.01234567},
        )

    assert response.status_code == 200
    assert response.content == (
        b'{"success":true,"btc_sold":0.01234567,'
        b'"status":"filled","pending":false}'
    )


def test_sell_bitcoin_rejects_invalid_or_excessive_amount_without_upstream_call() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Coinmate must not be called")

    with TestClient(create_app(SETTINGS, httpx.MockTransport(handler))) as client:
        excessive = client.post(
            "/sell_bitcoin",
            headers=AUTH,
            json={"amount": 0.50000001},
        )
        excessive_precision = client.post(
            "/sell_bitcoin",
            headers=AUTH,
            json={"amount": 0.000000001},
        )

    assert excessive.status_code == 422
    assert excessive.json() == {"detail": "amount exceeds MAX_MARKET_SELL_BTC"}
    assert excessive_precision.status_code == 422


def test_sell_idempotency_key_cannot_be_reused_for_another_amount() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sellInstant":
            return httpx.Response(
                200,
                json={"error": False, "errorMessage": None, "data": 321},
            )
        return httpx.Response(
            200,
            json={
                "error": False,
                "errorMessage": None,
                "data": {
                    "id": 321,
                    "type": "SELL",
                    "status": "FILLED",
                    "cumulativeAmount": 0.01,
                },
            },
        )

    with TestClient(create_app(SETTINGS, httpx.MockTransport(handler))) as client:
        first = client.post("/sell_bitcoin", headers=AUTH, json={"amount": 0.01})
        conflict = client.post("/sell_bitcoin", headers=AUTH, json={"amount": 0.02})

    assert first.status_code == 200
    assert conflict.status_code == 409


def test_sale_is_recovered_after_timeout_and_restart(tmp_path) -> None:
    sell_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sell_calls
        if request.url.path == "/api/sellInstant":
            sell_calls += 1
            raise httpx.ReadTimeout("unknown result", request=request)
        if request.url.path == "/api/order":
            return httpx.Response(
                200,
                json={"error": False, "errorMessage": None, "data": [{"id": 654}]},
            )
        assert request.url.path == "/api/orderById"
        return httpx.Response(
            200,
            json={
                "error": False,
                "errorMessage": None,
                "data": {
                    "id": 654,
                    "type": "SELL",
                    "status": "FILLED",
                    "cumulativeAmount": 0.01,
                },
            },
        )

    settings = replace(SETTINGS, database_path=str(tmp_path / "controller.db"))
    transport = httpx.MockTransport(handler)
    with TestClient(create_app(settings, transport)) as client:
        first = client.post("/sell_bitcoin", headers=AUTH, json={"amount": 0.01})

    assert first.status_code == 504

    with TestClient(create_app(settings, transport)) as client:
        recovered = client.get(
            f"/sell_bitcoin/{IDEMPOTENCY_KEY}",
            headers={"Authorization": "Bearer controller-secret"},
        )

    assert sell_calls == 1
    assert recovered.status_code == 200
    assert recovered.json() == {
        "success": True,
        "btc_sold": 0.01,
        "status": "filled",
        "pending": False,
    }
