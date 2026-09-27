from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import os


@dataclass(frozen=True)
class Settings:
    coinmate_client_id: str
    coinmate_public_key: str
    coinmate_private_key: str
    controller_api_token: str
    max_market_buy_czk: Decimal
    max_market_sell_btc: Decimal = Decimal("1")
    coinmate_api_url: str = "https://coinmate.io/api"
    balance_watch_timeout_seconds: float = 30.0
    balance_watch_poll_seconds: float = 5.0
    request_interval_seconds: float = 0.7
    purchase_reprice_seconds: float = 30.0
    purchase_poll_seconds: float = 5.0
    database_path: str = ":memory:"

    @classmethod
    def from_env(cls) -> "Settings":
        required = (
            "COINMATE_CLIENT_ID",
            "COINMATE_PUBLIC_KEY",
            "COINMATE_PRIVATE_KEY",
            "CONTROLLER_API_TOKEN",
        )
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        try:
            max_buy = Decimal(os.getenv("MAX_MARKET_BUY_CZK", "10000"))
        except InvalidOperation as exc:
            raise RuntimeError("MAX_MARKET_BUY_CZK must be a decimal number") from exc
        if max_buy <= 0:
            raise RuntimeError("MAX_MARKET_BUY_CZK must be greater than zero")

        try:
            max_sell = Decimal(os.getenv("MAX_MARKET_SELL_BTC", "1"))
        except InvalidOperation as exc:
            raise RuntimeError("MAX_MARKET_SELL_BTC must be a decimal number") from exc
        if max_sell <= 0:
            raise RuntimeError("MAX_MARKET_SELL_BTC must be greater than zero")

        try:
            watch_timeout = float(os.getenv("BALANCE_WATCH_TIMEOUT_SECONDS", "30"))
            watch_poll = float(os.getenv("BALANCE_WATCH_POLL_SECONDS", "5"))
        except ValueError as exc:
            raise RuntimeError("Balance watch intervals must be numbers") from exc
        if watch_timeout <= 0 or watch_poll <= 0 or watch_poll >= watch_timeout:
            raise RuntimeError(
                "Balance watch intervals must satisfy 0 < poll seconds < timeout seconds"
            )

        reprice = float(os.getenv("PURCHASE_REPRICE_SECONDS", "30"))
        poll = float(os.getenv("PURCHASE_POLL_SECONDS", "5"))
        if not (1 <= poll <= reprice <= 86400):
            raise RuntimeError("Purchase intervals must satisfy 1 <= poll <= reprice <= 86400")
        return cls(
            coinmate_client_id=os.environ["COINMATE_CLIENT_ID"],
            coinmate_public_key=os.environ["COINMATE_PUBLIC_KEY"],
            coinmate_private_key=os.environ["COINMATE_PRIVATE_KEY"],
            controller_api_token=os.environ["CONTROLLER_API_TOKEN"],
            max_market_buy_czk=max_buy,
            max_market_sell_btc=max_sell,
            coinmate_api_url=os.getenv("COINMATE_API_URL", "https://coinmate.io/api").rstrip("/"),
            balance_watch_timeout_seconds=watch_timeout,
            balance_watch_poll_seconds=watch_poll,
            purchase_reprice_seconds=reprice,
            purchase_poll_seconds=poll,
            database_path=os.getenv("DATABASE_PATH", "/data/coinmate-controller.db"),
        )
