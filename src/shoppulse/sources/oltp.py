"""Source 1 - the shop's operational (OLTP) SQLite database.

In a real company this database already exists. Here we *simulate* it with a
deterministic generator so the project runs anywhere and tests are repeatable.

The generator deliberately injects realistic dirty data (duplicate order ids,
missing emails, negative quantities, bad dates, unknown status values) so the
Transform and Data-Quality layers have real problems to solve.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from shoppulse.config import ORDER_STATUSES, Settings

log = logging.getLogger(__name__)

OLTP_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id INTEGER PRIMARY KEY,
    full_name   TEXT NOT NULL,
    email       TEXT,
    city        TEXT,
    country     TEXT,
    signup_date TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    order_id      INTEGER,
    customer_id   INTEGER NOT NULL,
    product_sku   TEXT NOT NULL,
    quantity      INTEGER,
    unit_price    REAL,
    currency      TEXT DEFAULT 'USD',
    order_date    TEXT,
    status        TEXT,
    updated_at    TEXT
);
"""

_FIRST = ["Ahmed", "Mona", "Omar", "Sara", "Youssef", "Nour", "Hassan", "Laila", "Karim", "Dina",
          "Tarek", "Salma", "Mostafa", "Heba", "Ali", "Farah", "Ibrahim", "Rana", "Khaled", "Maya"]
_LAST = ["Hassan", "Ali", "Mahmoud", "Saleh", "Farouk", "Nabil", "Adel", "Ezzat", "Fathy", "Sami"]
_CITIES = [("Cairo", "Egypt"), ("Alexandria", "Egypt"), ("Giza", "Egypt"), ("Fayoum", "Egypt"),
           ("Berlin", "Germany"), ("Munich", "Germany"), ("Riyadh", "Saudi Arabia"), ("Dubai", "UAE")]
SKUS = [f"SKU-{i:04d}" for i in range(1, 41)]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def session(path: Path):
    """Open a connection, commit on success, roll back on error and ALWAYS close it.

    (``with sqlite3.connect(...)`` alone only commits - it never closes the connection.)
    """
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _load_state(settings: Settings) -> dict:
    if settings.seed_state_file.exists():
        return json.loads(settings.seed_state_file.read_text())
    return {"day": 0}


def seed_oltp(settings: Settings, days: int = 30, orders_per_day: int = 25,
              seed: int = 42, dirty: bool = True, start: date | None = None) -> dict:
    """Create the OLTP DB (if needed) and add ``days`` more days of orders.

    Calling it again *continues* from where it stopped - that simulates a live
    shop and lets us demonstrate incremental loading.
    """
    settings.ensure_dirs()
    state = _load_state(settings)
    start = start or date(2026, 1, 1)
    first_day = state["day"]
    rng = random.Random(seed + first_day)

    with session(settings.oltp_db) as conn:
        conn.executescript(OLTP_SCHEMA)
        n_customers = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        if n_customers == 0:
            rows = []
            for cid in range(1, 201):
                city, country = rng.choice(_CITIES)
                name = f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
                email = f"{name.lower().replace(' ', '.')}{cid}@example.com"
                signup = (start - timedelta(days=rng.randint(10, 400))).isoformat()
                if dirty and cid % 17 == 0:
                    email = None  # missing email
                rows.append((cid, name, email, city, country, signup))
            conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", rows)

        next_order_id = (conn.execute("SELECT COALESCE(MAX(order_id),0) FROM orders").fetchone()[0]) + 1
        new_orders = []
        for d in range(first_day, first_day + days):
            day = start + timedelta(days=d)
            for _ in range(orders_per_day):
                oid = next_order_id
                next_order_id += 1
                sku = rng.choice(SKUS)
                qty = rng.randint(1, 5)
                price = round(rng.uniform(8, 60), 2)
                cur = rng.choices(["USD", "EUR", "GBP", "EGP"], weights=[60, 20, 10, 10])[0]
                if cur == "EGP":
                    price = round(price * 48, 2)
                status = rng.choices(ORDER_STATUSES, weights=[10, 20, 60, 10])[0]
                new_orders.append([oid, rng.randint(1, 200), sku, qty, price, cur,
                                   day.isoformat(), status, f"{day.isoformat()}T12:00:00"])

        if dirty and new_orders:
            # inject dirt into ~8% of the new rows
            for row in rng.sample(new_orders, k=max(1, len(new_orders) // 12)):
                kind = rng.choice(["neg_qty", "null_price", "bad_date", "bad_status", "case_status"])
                if kind == "neg_qty":
                    row[3] = -row[3]
                elif kind == "null_price":
                    row[4] = None
                elif kind == "bad_date":
                    row[6] = "31/02/2026"
                elif kind == "bad_status":
                    row[7] = "teleported"
                else:
                    row[7] = row[7].upper()
            # a few exact duplicate order ids (the same order sent twice)
            for row in rng.sample(new_orders, k=max(1, len(new_orders) // 40)):
                new_orders.append(list(row))

        conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", new_orders)

    settings.seed_state_file.write_text(json.dumps({"day": first_day + days}))
    log.info("OLTP seeded: +%d order rows (days %d..%d)", len(new_orders), first_day, first_day + days - 1)
    return {"orders_added": len(new_orders), "next_day": first_day + days}


def extract_customers(settings: Settings) -> pd.DataFrame:
    with session(settings.oltp_db) as conn:
        return pd.read_sql_query("SELECT * FROM customers", conn)


def extract_orders(settings: Settings, since: str | None = None) -> pd.DataFrame:
    """Incremental extraction: rows changed at or after the watermark ``since``.

    ``since`` is an ISO timestamp (the high-water mark saved by the previous run).
    We use ``>=`` on purpose: timestamps have 1-second resolution, so a row updated
    in the same second as the watermark could otherwise be missed. Re-reading the
    boundary rows is harmless because the warehouse load is an idempotent upsert
    (at-least-once delivery + idempotent load = effectively exactly-once).
    """
    query = "SELECT * FROM orders"
    params: tuple = ()
    if since:
        query += " WHERE updated_at >= ?"
        params = (since,)
    with session(settings.oltp_db) as conn:
        df = pd.read_sql_query(query, conn, params=params)
    log.info("Extracted %d orders (since=%s)", len(df), since)
    return df
