from decimal import Decimal
from dataclasses import replace
import hashlib
import hmac
import time
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
    coinmate_api_url="https://coinmate.test/api",
)
AUTH = {"Authorization": "Bearer controller-secret"}
IDEMPOTENCY_KEY = "550e8400-e29b-41d4-a716-446655440000"
BUY_AUTH = {**AUTH, "Idempotency-Key": IDEMPOTENCY_KEY}


def test_current_balance_returns_exact_json_number_and_signs_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/balances"
        nonce = request.headers["X-Coinmate-Nonce"]
        expected = hmac.new(
            b"private", f"{nonce}123public".encode(), hashlib.sha256
        ).hexdigest().upper()
        assert request.headers["X-Coinmate-Signature"] == expected
        form = parse_qs(request.content.decode())
        assert form == {
            "clientId": ["123"],
            "publicKey": ["public"],
            "nonce": [nonce],
            "signature": [expected],
        }
        return httpx.Response(
            200,
            json={
                "error": False,
                "errorMessage": None,
                "data": {
                    "CZK": {"balance": 1200.5, "reserved": 200, "available": 1000.5},
                    "BTC": {"balance": 0.01, "reserved": 0, "available": 0.01},
                },
            },
        )

    app = create_app(SETTINGS, httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.get("/current_balance/btc", headers=AUTH)

    assert response.status_code == 200
    assert response.content == b"0.01"
    assert response.json() == 0.01


def test_czk_balance_accepts_insignificant_trailing_decimal_places() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b'{"error":false,"errorMessage":null,'
                b'"data":{"CZK":{"balance":1250.50000000}}}'
            ),
            headers={"Content-Type": "application/json"},
        )

    app = create_app(SETTINGS, httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.get("/current_balance/czk", headers=AUTH)

    assert response.status_code == 200
    assert response.content == b"1250.50"


def test_czk_balance_rounds_sub_haler_amounts() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b'{"error":false,"errorMessage":null,'
                b'"data":{"CZK":{"balance":1250.50600000}}}'
            ),
            headers={"Content-Type": "application/json"},
        )

    app = create_app(SETTINGS, httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.get("/current_balance/czk", headers=AUTH)

    assert response.status_code == 200
    assert response.content == b"1250.51"


def test_buy_bitcoin_rejects_amount_above_safety_limit_without_upstream_call() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Coinmate must not be called")

    app = create_app(SETTINGS, httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/buy_bitcoin",
            headers=BUY_AUTH,
            json={"amount": 5000.01},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "amount exceeds MAX_MARKET_BUY_CZK"}


def test_private_endpoint_requires_token() -> None:
    app = create_app(SETTINGS, httpx.MockTransport(lambda _request: httpx.Response(500)))
    with TestClient(app) as client:
        response = client.get("/current_balance/czk")

    assert response.status_code == 401


def test_balance_watch_reports_changed_balance() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.url.path == "/api/balances"
        calls += 1
        balance = "0.10000000" if calls == 1 else "0.10000001"
        return httpx.Response(
            200,
            content=(
                '{"error":false,"errorMessage":null,"data":'
                '{"BTC":{"balance":' + balance + "}}}"
            ),
            headers={"Content-Type": "application/json"},
        )

    settings = replace(
        SETTINGS,
        balance_watch_timeout_seconds=0.2,
        balance_watch_poll_seconds=0.01,
    )
    app = create_app(settings, httpx.MockTransport(handler))
    with TestClient(app) as client:
        created = client.post("/balance_watch/btc", headers=AUTH)
        watch_id = created.json()["watch_id"]
        result = client.get(f"/balance_watch/{watch_id}", headers=AUTH)

    assert created.status_code == 200
    assert result.content == b'{"changed":true,"currency":"btc","balance":0.10000001}'


def test_balance_watch_normalizes_czk_trailing_decimal_places() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        balance = "1000.00000000" if calls == 1 else "1001.23000000"
        return httpx.Response(
            200,
            content=(
                '{"error":false,"errorMessage":null,"data":'
                '{"CZK":{"balance":' + balance + "}}}"
            ),
            headers={"Content-Type": "application/json"},
        )

    settings = replace(
        SETTINGS,
        balance_watch_timeout_seconds=0.2,
        balance_watch_poll_seconds=0.01,
    )
    app = create_app(settings, httpx.MockTransport(handler))
    with TestClient(app) as client:
        created = client.post("/balance_watch/czk", headers=AUTH)
        watch_id = created.json()["watch_id"]
        result = client.get(f"/balance_watch/{watch_id}", headers=AUTH)

    assert created.json()["initial_balance"] == 1000.00
    assert result.content == b'{"changed":true,"currency":"czk","balance":1001.23}'


def test_balance_watch_ping_renews_deadline_then_expires() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error": False,
                "errorMessage": None,
                "data": {"CZK": {"balance": 1000}},
            },
        )

    settings = replace(
        SETTINGS,
        balance_watch_timeout_seconds=0.06,
        balance_watch_poll_seconds=0.01,
    )
    app = create_app(settings, httpx.MockTransport(handler))
    with TestClient(app) as client:
        created = client.post("/balance_watch/czk", headers=AUTH)
        watch_id = created.json()["watch_id"]
        time.sleep(0.04)
        pinged = client.post(f"/balance_watch/{watch_id}/ping", headers=AUTH)
        wait_started = time.monotonic()
        result = client.get(f"/balance_watch/{watch_id}", headers=AUTH)
        wait_duration = time.monotonic() - wait_started

    assert pinged.status_code == 200
    assert wait_duration >= 0.04
    assert result.content == b'{"changed":false,"currency":"czk","balance":1000}'


