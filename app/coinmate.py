from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_DOWN, ROUND_CEILING
import asyncio
import hashlib
import hmac
import threading
import time
from typing import Any

import httpx

from app.config import Settings


class CoinmateError(Exception):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class CoinmateRejectedError(CoinmateError):
    pass


class CoinmateClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http_client
        self._last_nonce = 0
        self._nonce_lock = threading.Lock()
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0

    def _auth_headers(self) -> dict[str, str]:
        with self._nonce_lock:
            nonce = max(time.time_ns() // 1_000_000, self._last_nonce + 1)
            self._last_nonce = nonce

        message = f"{nonce}{self._settings.coinmate_client_id}{self._settings.coinmate_public_key}"
        signature = hmac.new(
            self._settings.coinmate_private_key.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest().upper()
        return {
            "Accept": "application/json",
            "X-Coinmate-Client-ID": self._settings.coinmate_client_id,
            "X-Coinmate-Nonce": str(nonce),
            "X-Coinmate-Public-Key": self._settings.coinmate_public_key,
            "X-Coinmate-Signature": signature,
        }

    async def _throttle(self) -> None:
        await asyncio.sleep(max(0, self._next_request_at - time.monotonic()))
        self._next_request_at = time.monotonic() + self._settings.request_interval_seconds

    async def _post(self, path: str, data: dict[str, str] | None = None) -> Any:
        async with self._request_lock:
            await self._throttle()
            return await self._post_direct(path, data)

    async def _post_direct(self, path: str, data: dict[str, str] | None = None) -> Any:
        headers = self._auth_headers()
        form = {
            "clientId": self._settings.coinmate_client_id,
            "publicKey": self._settings.coinmate_public_key,
            "nonce": headers["X-Coinmate-Nonce"],
            "signature": headers["X-Coinmate-Signature"],
            **(data or {}),
        }
        try:
            response = await self._http.post(path, headers=headers, data=form)
        except httpx.TimeoutException as exc:
            raise CoinmateError("Coinmate request timed out", 504) from exc
        except httpx.HTTPError as exc:
            raise CoinmateError("Coinmate is unavailable") from exc

        return self._response_data(response)

    @staticmethod
    def _response_data(response: httpx.Response) -> Any:
        try:
            payload = response.json(parse_float=Decimal)
        except ValueError as exc:
            raise CoinmateError("Coinmate returned an invalid response") from exc

        if response.status_code == 503:
            raise CoinmateError("Coinmate is temporarily unavailable", 503)
        if response.status_code == 429:
            raise CoinmateError("Coinmate rate limit exceeded", 503)
        if response.status_code in {400, 401, 403} and isinstance(payload, dict) and payload.get("error") is True:
            raise CoinmateRejectedError(str(payload.get("errorMessage") or "Coinmate rejected the request"))
        if not response.is_success or not isinstance(payload, dict):
            raise CoinmateError("Coinmate request failed")
        if payload.get("error") is not False:
            raise CoinmateRejectedError(
                str(payload.get("errorMessage") or "Coinmate rejected the request")
            )
        if "data" not in payload:
            raise CoinmateError("Coinmate response does not contain data")
        return payload["data"]

    async def balances(self) -> dict[str, Any]:
        data = await self._post("/balances")
        if not isinstance(data, dict):
            raise CoinmateError("Coinmate returned invalid balance data")
        return data

    @staticmethod
    def parse_decimal(value: Any, field: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise CoinmateError(f"Coinmate returned invalid {field}") from exc
        if not result.is_finite():
            raise CoinmateError(f"Coinmate returned invalid {field}")
        return result

    async def current_balance(self, currency: str) -> Decimal:
        balances = await self.balances()
        value = balances.get(currency.upper())
        if value is None:
            return Decimal(0)
        if not isinstance(value, dict) or "balance" not in value:
            raise CoinmateError("Coinmate returned invalid balance data")
        amount = self.parse_decimal(value["balance"], f"{currency.upper()} balance")
        decimals = 8 if currency.lower() == "btc" else 2
        if amount.as_tuple().exponent < -decimals:
            quantum = Decimal(1).scaleb(-decimals)
            amount = amount.quantize(quantum, rounding=ROUND_HALF_EVEN)
        return amount

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        async with self._request_lock:
            await self._throttle()
            return await self._get_direct(path, params)

    async def _get_direct(self, path: str, params: dict[str, str] | None = None) -> Any:
        try:
            response = await self._http.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise CoinmateError("Coinmate request timed out", 504) from exc
        except httpx.HTTPError as exc:
            raise CoinmateError("Coinmate is unavailable") from exc
        return self._response_data(response)

    async def maker_market(self) -> dict[str, Decimal]:
        pairs = await self._get("/tradingPairs")
        if not isinstance(pairs, list):
            raise CoinmateError("Invalid trading pairs")
        pair = next((p for p in pairs if isinstance(p, dict) and p.get("name") == "BTC_CZK"), None)
        if pair is None:
            raise CoinmateError("BTC_CZK is unavailable")
        for field, maximum in (("priceDecimals", 8), ("lotDecimals", 8)):
            if type(pair.get(field)) is not int or not 0 <= pair[field] <= maximum:
                raise CoinmateError("Invalid trading precision")
        minimum = self.parse_decimal(pair.get("minAmount"), "minimum amount")
        if minimum <= 0:
            raise CoinmateError("Invalid minimum amount")
        fees = await self._post("/traderFees")
        if not isinstance(fees, dict):
            raise CoinmateError("Invalid trading fees")
        maker = self.parse_decimal(fees.get("maker"), "maker fee")
        taker = self.parse_decimal(fees.get("taker"), "taker fee")
        if not 0 <= maker < 100 or not 0 <= taker < 100:
            raise CoinmateError("Invalid trading fees")
        if maker > Decimal("0.4"):
            raise CoinmateError("Maker fee exceeds 0.4%; purchase paused")
        # Conservative reservation for order admission; actual fills still use maker fees.
        fee_factor = 1 + max(maker, taker) / 100
        book = await self._get("/orderBook", {"currencyPair": "BTC_CZK", "groupByPriceLimit": "true"})
        if not isinstance(book, dict) or book.get("status") != "TRADING" or not book.get("asks") or not book.get("bids"):
            raise CoinmateError("BTC_CZK order book is unavailable")
        asks = book["asks"]
        bids = book["bids"]
        if (not isinstance(asks, list) or not all(isinstance(a, dict) for a in asks)
                or not isinstance(bids, list) or not all(isinstance(a, dict) for a in bids)):
            raise CoinmateError("Invalid order book")
        best_ask = min(self.parse_decimal(a.get("price"), "ask price") for a in asks)
        best_bid = max(self.parse_decimal(a.get("price"), "bid price") for a in bids)
        tick = Decimal(1).scaleb(-pair["priceDecimals"])
        # Join the best bid by one tick to get priority, while staying below the ask
        # so postOnly remains a maker order. A one-tick spread leaves no room to improve.
        price = min(best_bid + tick, best_ask - tick).quantize(tick, rounding=ROUND_DOWN)
        if price <= 0:
            raise CoinmateError("Invalid maker price")
        lot = Decimal(1).scaleb(-pair["lotDecimals"])
        minimum = minimum.quantize(lot, rounding=ROUND_CEILING)
        return {"price": price, "lot": lot, "minimum_btc": minimum, "fee_factor": fee_factor,
                "minimum_czk": (minimum * price * fee_factor + Decimal("0.01")).quantize(Decimal("0.01"), rounding=ROUND_CEILING)}

    async def maker_quote(self, budget: Decimal, ceiling: Decimal | None) -> dict[str, str] | None:
        market = await self.maker_market()
        price, lot, minimum = market["price"], market["lot"], market["minimum_btc"]
        if ceiling is not None:
            price = min(price, ceiling)
        # A haler also covers fee rounding. Finish only when the remaining budget
        # is dust, never merely because another order has reserved the balance.
        factor = market["fee_factor"]
        amount = (max(Decimal(0), budget - Decimal("0.01")) / factor / price).quantize(lot, rounding=ROUND_DOWN)
        if amount < minimum or amount <= 0:
            return None
        balances = await self.balances()
        czk = balances.get("CZK") if isinstance(balances, dict) else None
        if not isinstance(czk, dict):
            raise CoinmateError("Coinmate returned invalid CZK balance")
        available = self.parse_decimal(czk.get("available"), "available CZK")
        if available < 0:
            raise CoinmateError("Coinmate returned invalid available CZK")
        amount = min(amount, (max(Decimal(0), available - Decimal("0.01")) / factor / price).quantize(lot, rounding=ROUND_DOWN))
        if amount < minimum or amount <= 0:
            raise CoinmateError("Nedostatek dostupných CZK pro minimální objednávku včetně rezervy na poplatek.")
        return {"price": format(price, "f"), "amount": format(amount, "f")}

    async def place_limit_buy(self, quote: dict[str, str], client_order_id: str) -> int:
        order_id = await self._post("/buyLimit", {
            "currencyPair": "BTC_CZK", "price": quote["price"], "amount": quote["amount"],
            "postOnly": "1", "immediateOrCancel": "0", "clientOrderId": client_order_id,
        })
        if type(order_id) is not int or order_id <= 0:
            raise CoinmateError("Coinmate returned an invalid order ID")
        return order_id

    async def cancel_order(self, order_id: int) -> None:
        await self._post("/cancelOrder", {"orderId": str(order_id)})

    async def place_market_sell(
        self,
        btc_amount: Decimal,
        client_order_id: str,
    ) -> int:
        order_id = await self._post(
            "/sellInstant",
            {
                "currencyPair": "BTC_CZK",
                "amount": format(btc_amount, ".8f"),
                "clientOrderId": client_order_id,
            },
        )
        if not isinstance(order_id, int) or isinstance(order_id, bool):
            raise CoinmateError("Coinmate returned an invalid order ID")
        return order_id

    async def order_by_id(self, order_id: int) -> dict[str, Any]:
        order = await self._post("/orderById", {"orderId": str(order_id)})
        if not isinstance(order, dict):
            raise CoinmateError("Coinmate returned invalid order data")
        return order

    async def orders_by_client_order_id(self, client_order_id: str) -> list[dict[str, Any]]:
        orders = await self._post("/order", {"clientOrderId": client_order_id})
        if not isinstance(orders, list) or not all(isinstance(order, dict) for order in orders):
            raise CoinmateError("Coinmate returned invalid order lookup data")
        return orders
