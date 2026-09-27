"""Persist cancellation before starting the worker. Run only with service stopped."""
import argparse
import os
from pathlib import Path
from uuid import UUID

from app.purchase_store import PurchaseStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("id", type=UUID)
    args = parser.parse_args()
    path = os.getenv("DATABASE_PATH", "/data/coinmate-controller.db")
    if not Path(path).is_file():
        parser.error("Purchase database does not exist")
    store = PurchaseStore(path)
    try:
        try:
            record = store.request_maker_cancel(str(args.id))
        except (KeyError, ValueError) as exc:
            parser.error(f"Cannot cancel purchase: {exc}")
        print(f"{record.idempotency_key}: {record.status}")
        print("Cancellation intent saved. Start the controller to reconcile the exchange result.")
    finally:
        store.close()


if __name__ == "__main__":
    main()
