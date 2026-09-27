from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading


@dataclass(frozen=True)
class SaleRecord:
    idempotency_key: str
    amount_btc: str
    client_order_id: str
    status: str
    coinmate_order_id: int | None
    btc_sold: str
    coinmate_status: str | None


class SaleStore:
    def __init__(self, database_path: str) -> None:
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sales (
                    idempotency_key TEXT PRIMARY KEY,
                    amount_btc TEXT NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    coinmate_order_id INTEGER,
                    btc_sold TEXT NOT NULL DEFAULT '0',
                    coinmate_status TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );

                CREATE TABLE IF NOT EXISTS sale_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    FOREIGN KEY (idempotency_key) REFERENCES sales(idempotency_key)
                );
                """
            )

    def create_or_get(
        self,
        idempotency_key: str,
        amount_btc: str,
        client_order_id: str,
    ) -> tuple[SaleRecord, bool]:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM sales WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                return self._record(row), False

            self._connection.execute(
                """
                INSERT INTO sales
                    (idempotency_key, amount_btc, client_order_id, status)
                VALUES (?, ?, ?, 'CREATED')
                """,
                (idempotency_key, amount_btc, client_order_id),
            )
            self._connection.execute(
                """
                INSERT INTO sale_events (idempotency_key, event, detail)
                VALUES (?, 'CREATED', ?)
                """,
                (idempotency_key, f"amount_btc={amount_btc};client_order_id={client_order_id}"),
            )
            row = self._connection.execute(
                "SELECT * FROM sales WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to persist sale")
            return self._record(row), True

    def get(self, idempotency_key: str) -> SaleRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM sales WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            return None if row is None else self._record(row)

    def transition(
        self,
        idempotency_key: str,
        status: str,
        *,
        coinmate_order_id: int | None = None,
        btc_sold: str | None = None,
        coinmate_status: str | None = None,
        detail: str | None = None,
    ) -> SaleRecord:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE sales
                SET status = ?,
                    coinmate_order_id = COALESCE(?, coinmate_order_id),
                    btc_sold = COALESCE(?, btc_sold),
                    coinmate_status = COALESCE(?, coinmate_status),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE idempotency_key = ?
                """,
                (
                    status,
                    coinmate_order_id,
                    btc_sold,
                    coinmate_status,
                    idempotency_key,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO sale_events (idempotency_key, event, detail)
                VALUES (?, ?, ?)
                """,
                (idempotency_key, status, detail),
            )
            record = self.get(idempotency_key)
            if record is None:
                raise RuntimeError("Sale disappeared during transition")
            return record

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> SaleRecord:
        return SaleRecord(
            idempotency_key=row["idempotency_key"],
            amount_btc=row["amount_btc"],
            client_order_id=row["client_order_id"],
            status=row["status"],
            coinmate_order_id=row["coinmate_order_id"],
            btc_sold=row["btc_sold"],
            coinmate_status=row["coinmate_status"],
        )
