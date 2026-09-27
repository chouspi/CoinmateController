import asyncio
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import logging

from app.maker import MakerPurchases

from app.coinmate import CoinmateClient, CoinmateError
from app.purchase_store import PurchaseRecord, PurchaseStore


@dataclass(frozen=True)
class PurchaseOutcome:
    status: str
    success: bool
    btc_bought: Decimal
    pending: bool
    spent_czk: Decimal | None = None
    limit_price: Decimal | None = None
    detail: str | None = None
    completed_at: float | None = None


class PurchaseConflictError(Exception):
    pass


class PurchaseCoordinator:
    TERMINAL_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}

    def __init__(self, coinmate: CoinmateClient, store: PurchaseStore, reprice_seconds: float = 30, poll_seconds: float = 5) -> None:
        self._coinmate = coinmate
        self._store = store
        self._lock = asyncio.Lock()
        self._maker = MakerPurchases(coinmate, store, reprice_seconds)
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None

    async def buy(self, idempotency_key: str, amount: Decimal) -> PurchaseOutcome:
        amount_text = format(amount, ".2f")
        client_order_id = self._client_order_id(idempotency_key)

        async with self._lock:
            record, _ = self._store.create_or_get(
                idempotency_key, amount_text, client_order_id, self._maker.initial_state())
            if record.amount_czk != amount_text:
                raise PurchaseConflictError("Idempotency-Key was already used with a different amount")
            return self._outcome(record)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def tick(self) -> None:
        for key in self._store.active_keys():
            try:
                async with self._lock:
                    if self._store.maker_state(key) is not None:
                        await self._maker.step(key, self._client_order_id)
                # Legacy market orders are only reconciled, never replaced.
                if self._store.maker_state(key) is None:
                    await self.status(key)
            except CoinmateError:
                logging.getLogger(__name__).warning("Purchase %s is awaiting Coinmate reconciliation", key)
            except Exception:
                logging.getLogger(__name__).exception("Purchase worker failed for %s", key)

    async def _run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(self._poll_seconds)

    async def funding_balance(self) -> Decimal:
        """CZK balance plus our actual buy debits; own fills cannot hide a deposit.

        Take a stable exchange snapshot while submission/replacement is locked.
        An uncertain order or incomplete fills delay deposit recognition.
        """
        async with self._lock:
            for _ in range(3):
                before = await self._raw_czk_balance()
                spent = Decimal(0)
                for key in self._store.maker_keys():
                    state = self._store.maker_state(key)
                    if state is None:
                        continue
                    if state["attempts"]:
                        attempt = state["attempts"][-1]
                        if attempt["status"] not in {"CLOSED", "REJECTED"}:
                            if attempt["order_id"] is None:
                                orders = await self._coinmate.orders_by_client_order_id(attempt["client_id"])
                                if len(orders) != 1 or type(orders[0].get("id")) is not int:
                                    raise CoinmateError("Deposit check is waiting for an uncertain buy order")
                                attempt["order_id"] = orders[0]["id"]
                            order = await self._coinmate.order_by_id(attempt["order_id"])
                            self._maker._read_order(attempt, order)
                            self._maker._totals(state)
                            self._store.save_maker(key, state)
                    spent += Decimal(state["spent_czk"])
                after = await self._raw_czk_balance()
                if before == after:
                    return after + spent
            raise CoinmateError("Coinmate balance is changing; deposit check will retry")

    async def _raw_czk_balance(self) -> Decimal:
        data = await self._coinmate.balances()
        czk = data.get("CZK")
        if not isinstance(czk, dict):
            raise CoinmateError("Coinmate returned invalid CZK balance")
        return self._coinmate.parse_decimal(czk.get("balance"), "CZK balance")

    async def status(self, idempotency_key: str) -> PurchaseOutcome | None:
        async with self._lock:
            record = self._store.get(idempotency_key)
            if record is None:
                return None
            if self._store.maker_state(idempotency_key) is not None:
                return self._outcome(record)
            if record.status in self.TERMINAL_STATUSES:
                return self._outcome(record)
            if record.coinmate_order_id is None:
                record = await self._find_unknown_order(record)
                if record.coinmate_order_id is None:
                    return self._outcome(record)
            return await self._reconcile(record)

    async def cancel(self, key: str) -> PurchaseOutcome | None:
        async with self._lock:
            if self._store.get(key) is None:
                return None
            try:
                record = self._store.request_maker_cancel(key)
            except ValueError as exc:
                raise PurchaseConflictError(str(exc)) from exc
            return self._outcome(record)

    async def _find_unknown_order(self, record: PurchaseRecord) -> PurchaseRecord:
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

    async def _reconcile(self, record: PurchaseRecord) -> PurchaseOutcome:
        if record.coinmate_order_id is None:
            return self._outcome(record)
        order = await self._coinmate.order_by_id(record.coinmate_order_id)
        if order.get("id") != record.coinmate_order_id or order.get("type") != "BUY":
            raise CoinmateError("Coinmate returned an unexpected order")

        btc_bought = self._coinmate.parse_decimal(
            order.get("cumulativeAmount"),
            "filled BTC amount",
        )
        if btc_bought < 0 or btc_bought.as_tuple().exponent < -8:
            raise CoinmateError("Coinmate returned an invalid filled BTC amount")
        coinmate_status = order.get("status")
        if coinmate_status not in {"FILLED", "CANCELLED", "PARTIALLY_FILLED", "OPEN"}:
            raise CoinmateError("Coinmate returned an invalid order status")

        local_status = coinmate_status if coinmate_status in {"FILLED", "CANCELLED"} else "PLACED"
        record = self._store.transition(
            record.idempotency_key,
            local_status,
            btc_bought=format(btc_bought, "f"),
            coinmate_status=coinmate_status,
        )
        return self._outcome(record)

    @staticmethod
    def _client_order_id(idempotency_key: str) -> str:
        value = int.from_bytes(hashlib.sha256(idempotency_key.encode()).digest()[:8], "big")
        return str(value % 9_000_000_000_000_000_000 + 1)

    def _outcome(self, record: PurchaseRecord) -> PurchaseOutcome:
        btc_bought = Decimal(record.btc_bought)
        state = self._store.maker_state(record.idempotency_key)
        attempt = state["attempts"][-1] if state and state["attempts"] else None
        return PurchaseOutcome(
            status=record.status.lower(),
            success=record.status == "FILLED" and btc_bought > 0,
            btc_bought=btc_bought,
            pending=record.status not in PurchaseCoordinator.TERMINAL_STATUSES,
            spent_czk=Decimal(state["spent_czk"]) if state else None,
            limit_price=Decimal(attempt["price"]) if attempt else None,
            detail=state.get("error") if state else None,
            completed_at=state.get("completed_at") if state else None,
        )
