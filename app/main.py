from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
import secrets
import json
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Security, status
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import httpx
from pydantic import BaseModel, Field

from app.coinmate import CoinmateClient, CoinmateError
from app.balance_watch import BalanceWatchManager
from app.config import Settings
from app.purchase_store import PurchaseStore
from app.purchases import PurchaseConflictError, PurchaseCoordinator, PurchaseOutcome
from app.sale_store import SaleStore
from app.sales import SaleConflictError, SaleCoordinator, SaleOutcome


class BuyBitcoinRequest(BaseModel):
    amount: Annotated[Decimal, Field(gt=0, decimal_places=2)]


class SellBitcoinRequest(BaseModel):
    amount: Annotated[Decimal, Field(gt=0, decimal_places=8)]


PURCHASE_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["success", "btc_bought", "status", "pending"],
    "properties": {
        "success": {"type": "boolean"},
        "btc_bought": {"type": "number"},
        "status": {"type": "string"},
        "pending": {"type": "boolean"},
        "spent_czk": {"type": ["number", "null"]},
        "limit_price": {"type": ["number", "null"]},
        "detail": {"type": ["string", "null"]},
        "completed_at": {"type": ["number", "null"]},
    },
}

SALE_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["success", "btc_sold", "status", "pending"],
    "properties": {
        "success": {"type": "boolean"},
        "btc_sold": {"type": "number"},
        "status": {"type": "string"},
        "pending": {"type": "boolean"},
    },
}


def decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise CoinmateError("Coinmate returned a non-finite decimal value")
    return format(value, "f")


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings or Settings.from_env()
        async with httpx.AsyncClient(
            base_url=app.state.settings.coinmate_api_url,
            timeout=httpx.Timeout(10.0),
            transport=transport,
        ) as http_client:
            app.state.coinmate = CoinmateClient(app.state.settings, http_client)
            app.state.balance_watches = BalanceWatchManager(
                app.state.coinmate,
                app.state.settings.balance_watch_timeout_seconds,
                app.state.settings.balance_watch_poll_seconds,
            )
            app.state.purchase_store = PurchaseStore(app.state.settings.database_path)
            app.state.purchases = PurchaseCoordinator(
                app.state.coinmate,
                app.state.purchase_store,
                app.state.settings.purchase_reprice_seconds,
                app.state.settings.purchase_poll_seconds,
            )
            app.state.sale_store = SaleStore(app.state.settings.database_path)
            app.state.sales = SaleCoordinator(
                app.state.coinmate,
                app.state.sale_store,
            )
            app.state.purchases.start()
            try:
                yield
            finally:
                await app.state.purchases.close()
                await app.state.balance_watches.close()
                app.state.purchase_store.close()
                app.state.sale_store.close()

    app = FastAPI(
        title="Coinmate Controller",
        version="0.1.0",
        lifespan=lifespan,
    )
    bearer_scheme = HTTPBearer(auto_error=False)

    def authorize(
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(bearer_scheme),
        ] = None,
    ) -> None:
        expected = request.app.state.settings.controller_api_token
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not secrets.compare_digest(credentials.credentials, expected)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def client(request: Request) -> CoinmateClient:
        return request.app.state.coinmate

    def watches(request: Request) -> BalanceWatchManager:
        return request.app.state.balance_watches

    def purchases(request: Request) -> PurchaseCoordinator:
        return request.app.state.purchases

    def sales(request: Request) -> SaleCoordinator:
        return request.app.state.sales

    def purchase_response(outcome: PurchaseOutcome) -> Response:
        content = (
            f'{{"success":{str(outcome.success).lower()},'
            f'"btc_bought":{decimal_text(outcome.btc_bought)},'
            f'"status":"{outcome.status}",'
            f'"pending":{str(outcome.pending).lower()},'
            f'"spent_czk":{decimal_text(outcome.spent_czk) if outcome.spent_czk is not None else "null"},'
            f'"limit_price":{decimal_text(outcome.limit_price) if outcome.limit_price is not None else "null"},'
            f'"detail":{json.dumps(outcome.detail)},'
            f'"completed_at":{json.dumps(outcome.completed_at)}}}'
        )
        return Response(
            content=content,
            status_code=202 if outcome.pending else 200,
            media_type="application/json",
        )

    def sale_response(outcome: SaleOutcome) -> Response:
        content = (
            f'{{"success":{str(outcome.success).lower()},'
            f'"btc_sold":{decimal_text(outcome.btc_sold)},'
            f'"status":"{outcome.status}",'
            f'"pending":{str(outcome.pending).lower()}}}'
        )
        return Response(
            content=content,
            status_code=202 if outcome.pending else 200,
            media_type="application/json",
        )

    @app.exception_handler(CoinmateError)
    async def coinmate_error_handler(_request: Request, exc: CoinmateError) -> Any:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.get("/health", summary="Check service health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/current_balance/{currency}",
        summary="Get current CZK or BTC balance",
        description="Returns the total Coinmate balance directly as an exact JSON number.",
        dependencies=[Depends(authorize)],
        response_class=Response,
        responses={200: {"content": {"application/json": {"schema": {"type": "number"}}}}},
    )
    async def current_balance(
        currency: Literal["czk", "btc"],
        coinmate: Annotated[CoinmateClient, Depends(client)],
    ) -> Response:
        amount = await coinmate.current_balance(currency)
        return Response(content=decimal_text(amount), media_type="application/json")

    @app.get("/funding_balance/czk", dependencies=[Depends(authorize)])
    async def funding_balance(coordinator: Annotated[PurchaseCoordinator, Depends(purchases)]) -> Response:
        return Response(content=decimal_text(await coordinator.funding_balance()), media_type="application/json")

    @app.get("/buy_bitcoin/requirements", dependencies=[Depends(authorize)])
    async def purchase_requirements(
        request: Request, coinmate: Annotated[CoinmateClient, Depends(client)],
    ) -> Response:
        market = await coinmate.maker_market()
        return Response(content=(
            f'{{"min_amount_czk":{decimal_text(market["minimum_czk"])},'
            f'"min_amount_btc":{decimal_text(market["minimum_btc"])},'
            f'"max_amount_czk":{decimal_text(request.app.state.settings.max_market_buy_czk)}}}'
        ), media_type="application/json")

    @app.post(
        "/buy_bitcoin",
        summary="Queue a server-managed post-only BTC purchase",
        description=(
            "Uses the supplied CZK budget including fees for maker limit orders and returns whether the purchase completed "
            "and its actual cumulative BTC amount."
        ),
        dependencies=[Depends(authorize)],
        response_class=Response,
        responses={
            200: {
                "description": "The purchase reached a terminal state.",
                "content": {
                    "application/json": {
                        "schema": PURCHASE_RESPONSE_SCHEMA,
                    }
                }
            },
            202: {
                "description": "The purchase result is not safely known yet.",
                "content": {"application/json": {"schema": PURCHASE_RESPONSE_SCHEMA}},
            },
        },
    )
    async def buy_bitcoin(
        purchase: BuyBitcoinRequest,
        request: Request,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
        coordinator: Annotated[PurchaseCoordinator, Depends(purchases)],
    ) -> Response:
        if purchase.amount > request.app.state.settings.max_market_buy_czk:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="amount exceeds MAX_MARKET_BUY_CZK",
            )
        try:
            outcome = await coordinator.buy(str(idempotency_key), purchase.amount)
        except PurchaseConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return purchase_response(outcome)

    @app.get(
        "/buy_bitcoin/{idempotency_key}",
        dependencies=[Depends(authorize)],
        response_class=Response,
        responses={
            200: {
                "description": "The purchase reached a terminal state.",
                "content": {"application/json": {"schema": PURCHASE_RESPONSE_SCHEMA}},
            },
            202: {
                "description": "The purchase is pending or has an uncertain result.",
                "content": {"application/json": {"schema": PURCHASE_RESPONSE_SCHEMA}},
            },
        },
        summary="Reconcile or read a Bitcoin purchase",
        description=(
            "Returns a persisted result or reconciles an uncertain purchase with Coinmate "
            "without submitting another order."
        ),
    )
    async def bitcoin_purchase_status(
        idempotency_key: UUID,
        coordinator: Annotated[PurchaseCoordinator, Depends(purchases)],
    ) -> Response:
        outcome = await coordinator.status(str(idempotency_key))
        if outcome is None:
            raise HTTPException(status_code=404, detail="Purchase not found")
        return purchase_response(outcome)

    @app.post("/buy_bitcoin/{idempotency_key}/cancel", dependencies=[Depends(authorize)],
              summary="Stop a maker purchase and reconcile any existing order")
    async def cancel_bitcoin_purchase(
        idempotency_key: UUID,
        coordinator: Annotated[PurchaseCoordinator, Depends(purchases)],
    ) -> Response:
        try:
            outcome = await coordinator.cancel(str(idempotency_key))
        except PurchaseConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if outcome is None:
            raise HTTPException(status_code=404, detail="Purchase not found")
        return purchase_response(outcome)

    @app.post(
        "/sell_bitcoin",
        summary="Sell BTC at market price",
        description=(
            "Sells the supplied BTC amount and returns whether the order was fully filled "
            "and its actual cumulative sold BTC amount."
        ),
        dependencies=[Depends(authorize)],
        response_class=Response,
        responses={
            200: {
                "description": "The sale reached a terminal state.",
                "content": {"application/json": {"schema": SALE_RESPONSE_SCHEMA}},
            },
            202: {
                "description": "The sale result is not safely known yet.",
                "content": {"application/json": {"schema": SALE_RESPONSE_SCHEMA}},
            },
        },
    )
    async def sell_bitcoin(
        sale: SellBitcoinRequest,
        request: Request,
        idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
        coordinator: Annotated[SaleCoordinator, Depends(sales)],
    ) -> Response:
        if sale.amount > request.app.state.settings.max_market_sell_btc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="amount exceeds MAX_MARKET_SELL_BTC",
            )
        try:
            outcome = await coordinator.sell(str(idempotency_key), sale.amount)
        except SaleConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return sale_response(outcome)

    @app.get(
        "/sell_bitcoin/{idempotency_key}",
        dependencies=[Depends(authorize)],
        response_class=Response,
        responses={
            200: {
                "description": "The sale reached a terminal state.",
                "content": {"application/json": {"schema": SALE_RESPONSE_SCHEMA}},
            },
            202: {
                "description": "The sale is pending or has an uncertain result.",
                "content": {"application/json": {"schema": SALE_RESPONSE_SCHEMA}},
            },
        },
        summary="Reconcile or read a Bitcoin sale",
        description=(
            "Returns a persisted result or reconciles an uncertain sale with Coinmate "
            "without submitting another order."
        ),
    )
    async def bitcoin_sale_status(
        idempotency_key: UUID,
        coordinator: Annotated[SaleCoordinator, Depends(sales)],
    ) -> Response:
        outcome = await coordinator.status(str(idempotency_key))
        if outcome is None:
            raise HTTPException(status_code=404, detail="Sale not found")
        return sale_response(outcome)

    @app.post(
        "/balance_watch/{currency}",
        dependencies=[Depends(authorize)],
        summary="Start watching a balance",
        description=(
            "Captures the current balance and starts polling it. The watch expires after "
            "the configured heartbeat window (30 seconds by default) unless renewed."
        ),
    )
    async def create_balance_watch(
        currency: Literal["czk", "btc"],
        manager: Annotated[BalanceWatchManager, Depends(watches)],
    ) -> Response:
        watch = await manager.create(currency)
        return Response(
            content=(
                f'{{"watch_id":"{watch.watch_id}","currency":"{currency}",'
                f'"initial_balance":{decimal_text(watch.initial_balance)},'
                f'"expires_in_seconds":{app.state.settings.balance_watch_timeout_seconds:g}}}'
            ),
            media_type="application/json",
        )

    @app.post(
        "/balance_watch/{watch_id}/ping",
        dependencies=[Depends(authorize)],
        summary="Renew a balance watch",
        description="Extends an active watch by the configured heartbeat window.",
    )
    async def ping_balance_watch(
        watch_id: str,
        request: Request,
        manager: Annotated[BalanceWatchManager, Depends(watches)],
    ) -> dict[str, str | float]:
        if not manager.ping(watch_id):
            raise HTTPException(status_code=404, detail="Active balance watch not found")
        return {
            "watch_id": watch_id,
            "expires_in_seconds": request.app.state.settings.balance_watch_timeout_seconds,
        }

    @app.get(
        "/balance_watch/{watch_id}",
        dependencies=[Depends(authorize)],
        summary="Wait for a watched balance to change",
        description=(
            "Waits until the balance changes or the heartbeat deadline expires. "
            "The result consumes the watch."
        ),
    )
    async def wait_for_balance_change(
        watch_id: str,
        manager: Annotated[BalanceWatchManager, Depends(watches)],
    ) -> Response:
        watch = await manager.wait(watch_id)
        if watch is None:
            raise HTTPException(status_code=404, detail="Balance watch not found")
        return Response(
            content=(
                f'{{"changed":{str(watch.changed).lower()},'
                f'"currency":"{watch.currency}",'
                f'"balance":{decimal_text(watch.balance)}}}'
            ),
            media_type="application/json",
        )

    return app


app = create_app()
