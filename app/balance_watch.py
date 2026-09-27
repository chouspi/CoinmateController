import asyncio
from dataclasses import dataclass
from decimal import Decimal
import time
from uuid import uuid4

from app.coinmate import CoinmateClient, CoinmateError


@dataclass
class BalanceWatch:
    watch_id: str
    currency: str
    initial_balance: Decimal
    balance: Decimal
    deadline: float
    event: asyncio.Event
    changed: bool = False
    task: asyncio.Task[None] | None = None


class BalanceWatchManager:
    def __init__(
        self,
        coinmate: CoinmateClient,
        timeout_seconds: float,
        poll_seconds: float,
    ) -> None:
        self._coinmate = coinmate
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = poll_seconds
        self._watches: dict[str, BalanceWatch] = {}

    async def create(self, currency: str) -> BalanceWatch:
        active_count = sum(not watch.event.is_set() for watch in self._watches.values())
        if active_count >= 4:
            raise CoinmateError("Maximum number of active balance watches reached", 429)

        balance = await self._coinmate.current_balance(currency)
        watch = BalanceWatch(
            watch_id=str(uuid4()),
            currency=currency,
            initial_balance=balance,
            balance=balance,
            deadline=time.monotonic() + self._timeout_seconds,
            event=asyncio.Event(),
        )
        self._watches[watch.watch_id] = watch
        watch.task = asyncio.create_task(self._run(watch))
        return watch

    def ping(self, watch_id: str) -> bool:
        watch = self._watches.get(watch_id)
        if watch is None or watch.event.is_set():
            return False
        watch.deadline = time.monotonic() + self._timeout_seconds
        return True

    async def wait(self, watch_id: str) -> BalanceWatch | None:
        watch = self._watches.get(watch_id)
        if watch is None:
            return None
        await watch.event.wait()
        self._watches.pop(watch_id, None)
        return watch

    async def close(self) -> None:
        tasks = [watch.task for watch in self._watches.values() if watch.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._watches.clear()

    async def _run(self, watch: BalanceWatch) -> None:
        while True:
            remaining = watch.deadline - time.monotonic()
            if remaining <= 0:
                self._finish(watch)
                return

            await asyncio.sleep(min(self._poll_seconds, remaining))
            if time.monotonic() >= watch.deadline:
                self._finish(watch)
                return

            try:
                balance = await self._coinmate.current_balance(watch.currency)
            except CoinmateError:
                # A transient upstream error must not be mistaken for a balance change.
                continue
            watch.balance = balance
            if balance != watch.initial_balance:
                watch.changed = True
                self._finish(watch)
                return

    def _finish(self, watch: BalanceWatch) -> None:
        watch.event.set()
        asyncio.get_running_loop().call_later(
            self._timeout_seconds,
            self._discard_completed,
            watch.watch_id,
            watch,
        )

    def _discard_completed(self, watch_id: str, watch: BalanceWatch) -> None:
        if self._watches.get(watch_id) is watch and watch.event.is_set():
            self._watches.pop(watch_id, None)
