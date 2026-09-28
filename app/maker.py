"""Durable post-only buy workflow. Every exchange mutation has a saved intent."""
import time
from decimal import Decimal

from app.coinmate import CoinmateClient, CoinmateError, CoinmateRejectedError
from app.purchase_store import PurchaseStore


class MakerPurchases:
    def __init__(self, coinmate: CoinmateClient, store: PurchaseStore, reprice_seconds: float = 30):
        self.coinmate = coinmate
        self.store = store
        self.reprice_seconds = reprice_seconds

    @staticmethod
    def initial_state() -> dict:
        return {"attempts": [], "spent_czk": "0", "btc_bought": "0", "completed_at": None,
                "error": None, "next_attempt_at": 0}

    async def step(self, key: str, client_id) -> None:
        state = self.store.maker_state(key)
        record = self.store.get(key)
        if state is None or record is None or record.status in {"FILLED", "CANCELLED", "REJECTED"}:
            return
        try:
            await self._step(key, record, state, client_id)
        except CoinmateError as exc:
            state["error"] = str(exc)
            raise
        finally:
            self.store.save_maker(key, state)

    async def _step(self, key, record, state, client_id):
        attempts = state["attempts"]
        if attempts and attempts[-1]["status"] not in {"CLOSED", "REJECTED"}:
            attempt = attempts[-1]
            if attempt["order_id"] is None:
                # An absent lookup is NOT proof a timed-out submit never arrived.
                orders = await self.coinmate.orders_by_client_order_id(attempt["client_id"])
                if not orders:
                    state["error"] = "Výsledek odeslání není známý; ověřujeme původní objednávku."
                    return
                if len(orders) != 1 or type(orders[0].get("id")) is not int:
                    raise CoinmateError("Ambiguous order lookup")
                attempt["order_id"] = orders[0]["id"]
                self.store.save_maker(key, state)
            order = await self.coinmate.order_by_id(attempt["order_id"])
            self._read_order(attempt, order)
            self._totals(state)
            state["error"] = None
            self.store.transition(key, "CANCELLING" if state.get("cancel_requested") else "PLACED",
                                  btc_bought=state["btc_bought"])
            if order["status"] not in {"FILLED", "CANCELLED"}:
                if state.get("cancel_requested") or time.time() - attempt["placed_at"] >= self.reprice_seconds:
                    # Cancellation may race with a fill. Only a later terminal order
                    # snapshot allows the next attempt, including after a restart.
                    attempt["status"] = "CANCELLING"
                    self.store.save_maker(key, state)
                    await self.coinmate.cancel_order(attempt["order_id"])
                return
            attempt["status"] = "CLOSED"
            self.store.save_maker(key, state)

        if state.get("cancel_requested"):
            state["error"] = None
            state["completed_at"] = state.get("last_fill_at") or time.time()
            self.store.finish_maker(key, state, "CANCELLED", "Cancellation requested by operator")
            return
        if time.time() < state["next_attempt_at"]:
            return
        remaining = Decimal(record.amount_czk) - Decimal(state["spent_czk"])
        quote = await self.coinmate.maker_quote(remaining, None)
        if quote is None:
            state["error"] = None
            filled = Decimal(state["btc_bought"]) > 0
            state["completed_at"] = state.get("last_fill_at") or time.time()
            self.store.finish_maker(key, state, "FILLED" if filled else "REJECTED")
            return
        attempt = {**quote, "client_id": client_id(f"{key}:maker:{len(attempts)}"),
                   "order_id": None, "status": "SUBMITTING", "placed_at": time.time(),
                   "btc_bought": "0", "spent_czk": "0"}
        attempts.append(attempt)
        # Save BEFORE sending. Never automatically resubmit an uncertain request.
        self.store.save_maker(key, state)
        self.store.transition(key, "SUBMITTING", detail=f"postOnly=1;price={quote['price']};amount={quote['amount']}")
        try:
            attempt["order_id"] = await self.coinmate.place_limit_buy(quote, attempt["client_id"])
        except CoinmateRejectedError as exc:
            # Only a confirmed rejection is safe to retry; pace retries (e.g. post-only races).
            attempt["status"] = "REJECTED"
            state["error"] = str(exc)
            state["next_attempt_at"] = time.time() + self.reprice_seconds
            return
        attempt["status"] = "OPEN"
        state["error"] = None
        self.store.transition(key, "PLACED", detail=f"order_id={attempt['order_id']}")

    def _read_order(self, attempt, order):
        if order.get("id") != attempt["order_id"] or order.get("type") != "BUY":
            raise CoinmateError("Unexpected buy order")
        status = order.get("status")
        if status not in {"FILLED", "CANCELLED", "OPEN", "PARTIALLY_FILLED"}:
            raise CoinmateError("Invalid buy order status")
        trades = order.get("trades", [])
        if not isinstance(trades, list):
            raise CoinmateError("Invalid order trades")
        raw_amount = order.get("cumulativeAmount")
        if raw_amount in (None, ""):
            if status == "OPEN" or (status == "CANCELLED" and not trades):
                raw_amount = "0"
            else:
                raise CoinmateError("Coinmate returned invalid filled BTC")
        amount = self.coinmate.parse_decimal(raw_amount, "filled BTC")
        if amount != amount.quantize(Decimal("0.00000001")) or amount < Decimal(attempt["btc_bought"]) or amount > Decimal(attempt["amount"]) or amount < 0:
            raise CoinmateError("Invalid filled amount")
        total_amount, total_cost, latest = Decimal(0), Decimal(0), 0
        seen = set()
        for trade in trades:
            if not isinstance(trade, dict) or trade.get("currencyPair") != "BTC_CZK" or trade.get("orderId") != attempt["order_id"]:
                raise CoinmateError("Unexpected fill")
            trade_id = trade.get("transactionId")
            if type(trade_id) is not int or trade_id in seen:
                raise CoinmateError("Invalid or duplicate fill")
            seen.add(trade_id)
            quantity = self.coinmate.parse_decimal(trade.get("amount"), "trade amount")
            price = self.coinmate.parse_decimal(trade.get("price"), "trade price")
            fee = self.coinmate.parse_decimal(trade.get("fee"), "trade fee")
            timestamp = self.coinmate.parse_decimal(trade.get("createdTimestamp"), "trade timestamp")
            if quantity <= 0 or price <= 0 or timestamp <= 0 or trade.get("feeType") != "MAKER":
                raise CoinmateError("Invalid maker fill")
            total_amount += quantity
            total_cost += quantity * price + fee  # Coinmate trade fees are in the quote currency (CZK).
            latest = max(latest, float(timestamp / 1000))
        if total_amount != amount or total_cost < 0:
            raise CoinmateError("Order trades are incomplete; waiting for authoritative fills")
        attempt.update(btc_bought=format(amount, "f"), spent_czk=format(total_cost, "f"), last_fill_at=latest)

    @staticmethod
    def _totals(state):
        state["btc_bought"] = str(sum((Decimal(a["btc_bought"]) for a in state["attempts"]), Decimal(0)))
        state["spent_czk"] = str(sum((Decimal(a["spent_czk"]) for a in state["attempts"]), Decimal(0)))
        state["last_fill_at"] = max((a.get("last_fill_at", 0) for a in state["attempts"]), default=0)
