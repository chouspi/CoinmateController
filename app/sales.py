import asyncio
from dataclasses import dataclass
from decimal import Decimal
import hashlib

from app.coinmate import CoinmateClient, CoinmateError, CoinmateRejectedError
from app.sale_store import SaleRecord, SaleStore


@dataclass(frozen=True)
class SaleOutcome:
    status: str
    success: bool
    btc_sold: Decimal
    pending: bool


class SaleConflictError(Exception):
    pass


class SaleCoordinator:
    TERMINAL_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}

    def __init__(self, coinmate: CoinmateClient, store: SaleStore) -> None:
        self._coinmate = coinmate
        self._store = store
        self._lock = asyncio.Lock()

    async def sell(self, idempotency_key: str, amount: Decimal) -> SaleOutcome:
        amount_text = format(amount, ".8f")
        client_order_id = self._client_order_id(idempotency_key)

        async with self._lock:
            record, created = self._store.create_or_get(
                idempotency_key,
                amount_text,
                client_order_id,
            )
            if record.amount_btc != amount_text:
                raise SaleConflictError(
                    "Idempotency-Key was already used with a different amount"
                )
            if record.status in self.TERMINAL_STATUSES:
                return self._outcome(record)

            if created:
                self._store.transition(idempotency_key, "SUBMITTING")
                try:
                    order_id = await self._coinmate.place_market_sell(amount, client_order_id)
                except CoinmateRejectedError as exc:
                    record = self._store.transition(
                        idempotency_key,
                        "REJECTED",
                        detail=str(exc),
                    )
                    return self._outcome(record)
                except CoinmateError as exc:
                    self._store.transition(idempotency_key, "UNKNOWN", detail=str(exc))
                    raise
                record = self._store.transition(
                    idempotency_key,
                    "PLACED",
                    coinmate_order_id=order_id,
                )
            elif record.coinmate_order_id is None:
                record = await self._find_unknown_order(record)
                if record.coinmate_order_id is None:
                    return self._outcome(record)

            return await self._reconcile(record)

    async def status(self, idempotency_key: str) -> SaleOutcome | None:
        async with self._lock:
            record = self._store.get(idempotency_key)
            if record is None:
                return None
            if record.status in self.TERMINAL_STATUSES:
                return self._outcome(record)
            if record.coinmate_order_id is None:
                record = await self._find_unknown_order(record)
                if record.coinmate_order_id is None:
                    return self._outcome(record)
            return await self._reconcile(record)

    async def _find_unknown_order(self, record: SaleRecord) -> SaleRecord:
        orders = await self._coinmate.orders_by_client_order_id(record.client_order_id)
        if not orders:
            return self._store.transition(
                record.idempotency_key,
                "UNKNOWN",
                detail="No matching Coinmate order found; automatic resubmission disabled",
            )
        if len(orders) != 1 or not isinstance(orders[0].get("id"), int):
            raise CoinmateError("Coinmate returned an ambiguous order lookup result")
        return self._store.transition(
            record.idempotency_key,
            "PLACED",
            coinmate_order_id=orders[0]["id"],
            detail="Recovered by clientOrderId",
        )

    async def _reconcile(self, record: SaleRecord) -> SaleOutcome:
        if record.coinmate_order_id is None:
            return self._outcome(record)
        order = await self._coinmate.order_by_id(record.coinmate_order_id)
        if order.get("id") != record.coinmate_order_id or order.get("type") != "SELL":
            raise CoinmateError("Coinmate returned an unexpected order")

        btc_sold = self._coinmate.parse_decimal(
            order.get("cumulativeAmount"),
            "sold BTC amount",
        )
        if btc_sold < 0 or btc_sold.as_tuple().exponent < -8:
            raise CoinmateError("Coinmate returned an invalid sold BTC amount")
        coinmate_status = order.get("status")
        if coinmate_status not in {"FILLED", "CANCELLED", "PARTIALLY_FILLED", "OPEN"}:
            raise CoinmateError("Coinmate returned an invalid order status")

        local_status = coinmate_status if coinmate_status in {"FILLED", "CANCELLED"} else "PLACED"
        record = self._store.transition(
            record.idempotency_key,
            local_status,
            btc_sold=format(btc_sold, "f"),
            coinmate_status=coinmate_status,
        )
        return self._outcome(record)

    @staticmethod
    def _client_order_id(idempotency_key: str) -> str:
        digest = hashlib.sha256(f"sell:{idempotency_key}".encode()).digest()
        value = int.from_bytes(digest[:8], "big")
        return str(value % 9_000_000_000_000_000_000 + 1)

    @staticmethod
    def _outcome(record: SaleRecord) -> SaleOutcome:
        btc_sold = Decimal(record.btc_sold)
        return SaleOutcome(
            status=record.status.lower(),
            success=record.status == "FILLED" and btc_sold > 0,
            btc_sold=btc_sold,
            pending=record.status not in SaleCoordinator.TERMINAL_STATUSES,
        )
