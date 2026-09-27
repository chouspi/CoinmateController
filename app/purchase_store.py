from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3
import threading


@dataclass(frozen=True)
class PurchaseRecord:
    idempotency_key: str
    amount_czk: str
    client_order_id: str
    status: str
    coinmate_order_id: int | None
    btc_bought: str
    coinmate_status: str | None


class PurchaseStore:
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
                CREATE TABLE IF NOT EXISTS purchases (
                    idempotency_key TEXT PRIMARY KEY,
                    amount_czk TEXT NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    coinmate_order_id INTEGER,
                    btc_bought TEXT NOT NULL DEFAULT '0',
                    coinmate_status TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );

                CREATE TABLE IF NOT EXISTS maker_runs (
                    idempotency_key TEXT PRIMARY KEY,
                    state TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS purchase_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    FOREIGN KEY (idempotency_key) REFERENCES purchases(idempotency_key)
                );
                """
            )

    def create_or_get(
        self,
        idempotency_key: str,
        amount_czk: str,
        client_order_id: str,
        maker_state: dict | None = None,
    ) -> tuple[PurchaseRecord, bool]:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM purchases WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                return self._record(row), False

            self._connection.execute(
                """
                INSERT INTO purchases
                    (idempotency_key, amount_czk, client_order_id, status)
                VALUES (?, ?, ?, 'CREATED')
                """,
                (idempotency_key, amount_czk, client_order_id),
            )
            if maker_state is not None:
                self._connection.execute("INSERT INTO maker_runs VALUES (?, ?)",
                    (idempotency_key, json.dumps(maker_state)))
            self._connection.execute(
                """
                INSERT INTO purchase_events (idempotency_key, event, detail)
                VALUES (?, 'CREATED', ?)
                """,
                (idempotency_key, f"amount_czk={amount_czk};client_order_id={client_order_id}"),
            )
            row = self._connection.execute(
                "SELECT * FROM purchases WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to persist purchase")
            return self._record(row), True

    def get(self, idempotency_key: str) -> PurchaseRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM purchases WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            return None if row is None else self._record(row)

    def transition(
        self,
        idempotency_key: str,
        status: str,
        *,
        coinmate_order_id: int | None = None,
        btc_bought: str | None = None,
        coinmate_status: str | None = None,
        detail: str | None = None,
    ) -> PurchaseRecord:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE purchases
                SET status = ?,
                    coinmate_order_id = COALESCE(?, coinmate_order_id),
                    btc_bought = COALESCE(?, btc_bought),
                    coinmate_status = COALESCE(?, coinmate_status),
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE idempotency_key = ?
                """,
                (
                    status,
                    coinmate_order_id,
                    btc_bought,
                    coinmate_status,
                    idempotency_key,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO purchase_events (idempotency_key, event, detail)
                VALUES (?, ?, ?)
                """,
                (idempotency_key, status, detail),
            )
            record = self.get(idempotency_key)
            if record is None:
                raise RuntimeError("Purchase disappeared during transition")
            return record

    def maker_state(self, key: str) -> dict | None:
        with self._lock:
            row = self._connection.execute("SELECT state FROM maker_runs WHERE idempotency_key = ?", (key,)).fetchone()
            return None if row is None else json.loads(row[0])

    def save_maker(self, key: str, state: dict) -> None:
        with self._lock, self._connection:
            self._connection.execute("UPDATE maker_runs SET state = ? WHERE idempotency_key = ?",
                                     (json.dumps(state), key))

    def finish_maker(self, key: str, state: dict, status: str) -> None:
        # The terminal marker and the accounting result must survive a crash together.
        with self._lock, self._connection:
            self._connection.execute("UPDATE maker_runs SET state=? WHERE idempotency_key=?", (json.dumps(state), key))
            self._connection.execute("UPDATE purchases SET status=?, btc_bought=? WHERE idempotency_key=?",
                                     (status, state["btc_bought"], key))
            self._connection.execute("INSERT INTO purchase_events(idempotency_key,event,detail) VALUES(?,?,?)",
                                     (key, status, "Remaining budget below exchange minimum"))

    def maker_keys(self) -> list[str]:
        with self._lock:
            return [row[0] for row in self._connection.execute("SELECT idempotency_key FROM maker_runs")]

    def active_keys(self) -> list[str]:
        with self._lock:
            return [row[0] for row in self._connection.execute(
                "SELECT idempotency_key FROM purchases WHERE status NOT IN ('FILLED', 'CANCELLED', 'REJECTED')")]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> PurchaseRecord:
        return PurchaseRecord(
            idempotency_key=row["idempotency_key"],
            amount_czk=row["amount_czk"],
            client_order_id=row["client_order_id"],
            status=row["status"],
            coinmate_order_id=row["coinmate_order_id"],
            btc_bought=row["btc_bought"],
            coinmate_status=row["coinmate_status"],
        )
