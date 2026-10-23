"""Data Warehouse loading: star schema, SCD Type 2, incremental fact loads.

Everything here is *idempotent*: running the same load twice leaves the
warehouse in exactly the same state (no duplicated rows, no extra versions).
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from shoppulse.utils import row_hash, utc_now_iso

log = logging.getLogger(__name__)
SQL_DIR = Path(__file__).parent / "sql"
OPEN_END = "9999-12-31"
BEGINNING = "1900-01-01"


# ---------------------------------------------------------------- connection
def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript((SQL_DIR / "ddl.sql").read_text(encoding="utf-8"))
    conn.executescript((SQL_DIR / "views.sql").read_text(encoding="utf-8"))
    ensure_dim_date(conn)
    conn.commit()


def ensure_dim_date(conn: sqlite3.Connection, start: str = "2025-01-01", end: str = "2030-12-31") -> None:
    if conn.execute("SELECT COUNT(*) FROM dim_date").fetchone()[0]:
        return
    days = pd.date_range(start, end, freq="D")
    rows = [
        (int(d.strftime("%Y%m%d")), d.date().isoformat(), d.year, (d.month - 1) // 3 + 1, d.month,
         d.strftime("%B"), d.day, d.dayofweek, d.strftime("%A"), int(d.dayofweek >= 5))
        for d in days
    ]
    conn.executemany("INSERT INTO dim_date VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


# ---------------------------------------------------------------------- SCD2
def scd2_upsert(conn: sqlite3.Connection, table: str, key: str, df: pd.DataFrame,
                tracked: list[str], attrs: list[str], as_of: str) -> dict:
    """Slowly Changing Dimension Type 2.

    * new business key        -> insert a current row (valid since the beginning of time)
    * tracked attribute moved -> close the old row (valid_to = as_of) and insert a new current row
    * nothing changed         -> do nothing
    ``table``/``key`` are internal constants (never user input), so f-strings are safe here.
    """
    if df.empty:
        return {"new": 0, "changed": 0, "unchanged": 0}

    incoming = df.copy()
    incoming["row_hash"] = [row_hash(*vals) for vals in incoming[tracked].itertuples(index=False, name=None)]
    current = {
        r[key]: (r["row_hash"], r[f"{table}_surrogate"])
        for r in conn.execute(
            f"SELECT {key}, row_hash, rowid AS {table}_surrogate FROM {table} WHERE is_current = 1 AND {key} NOT IN (-1, 'UNKNOWN')"
        )
    }

    new_rows, changed_rows, unchanged = [], [], 0
    for rec in incoming.to_dict("records"):
        existing = current.get(rec[key])
        if existing is None:
            new_rows.append((rec, BEGINNING))
        elif existing[0] != rec["row_hash"]:
            changed_rows.append((rec, as_of, existing[1]))
        else:
            unchanged += 1

    cols = [key, *attrs, "row_hash", "valid_from"]
    insert_sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"

    def values(rec: dict, valid_from: str) -> list:
        return [None if (isinstance(rec.get(c), float) and rec[c] != rec[c]) else rec.get(c) for c in [key, *attrs, "row_hash"]] + [valid_from]

    for _rec, _valid_from, rowid in changed_rows:
        conn.execute(f"UPDATE {table} SET valid_to = ?, is_current = 0 WHERE rowid = ?", (as_of, rowid))
    conn.executemany(insert_sql, [values(r, vf) for r, vf in new_rows])
    conn.executemany(insert_sql, [values(r, vf) for r, vf, _ in changed_rows])

    stats = {"new": len(new_rows), "changed": len(changed_rows), "unchanged": unchanged}
    log.info("SCD2 %s: %s", table, stats)
    return stats


def load_dim_customer(conn: sqlite3.Connection, customers: pd.DataFrame, as_of: str | None = None) -> dict:
    return scd2_upsert(
        conn, "dim_customer", "customer_id", customers,
        tracked=["full_name", "email", "city", "country"],
        attrs=["full_name", "email", "city", "country", "signup_date"],
        as_of=as_of or date.today().isoformat(),
    )


def load_dim_product(conn: sqlite3.Connection, products: pd.DataFrame, as_of: str | None = None) -> dict:
    return scd2_upsert(
        conn, "dim_product", "product_sku", products,
        tracked=["title", "category", "price_gbp", "price_band", "rating", "in_stock"],
        attrs=["title", "category", "price_gbp", "price_band", "rating", "in_stock", "url"],
        as_of=as_of or date.today().isoformat(),
    )


# ---------------------------------------------------------------------- facts
def load_fact_orders(conn: sqlite3.Connection, orders: pd.DataFrame) -> int:
    """Upsert orders. The dimension keys are resolved *as of the order date*
    (point-in-time join), so an old order keeps pointing at the customer's old city."""
    if orders.empty:
        return 0
    orders.to_sql("stg_orders", conn, if_exists="replace", index=False)
    now = utc_now_iso()
    before = conn.execute("SELECT COUNT(*) FROM fact_orders").fetchone()[0]
    conn.execute(
        """
        INSERT INTO fact_orders (order_id, customer_key, product_key, date_key, quantity,
                                 unit_price_usd, total_usd, currency, status, order_date, updated_at, loaded_at)
        SELECT s.order_id,
               COALESCE(c.customer_key, -1),
               COALESCE(p.product_key, -1),
               CAST(strftime('%Y%m%d', s.order_date) AS INTEGER),
               s.quantity, s.unit_price_usd, s.total_usd, s.currency, s.status, s.order_date, s.updated_at, ?
        FROM stg_orders s
        LEFT JOIN dim_customer c ON c.customer_id = s.customer_id
                                AND s.order_date >= c.valid_from AND s.order_date < c.valid_to
        LEFT JOIN dim_product  p ON p.product_sku = s.product_sku
                                AND s.order_date >= p.valid_from AND s.order_date < p.valid_to
        WHERE true
        ON CONFLICT(order_id) DO UPDATE SET
            status         = excluded.status,
            quantity       = excluded.quantity,
            unit_price_usd = excluded.unit_price_usd,
            total_usd      = excluded.total_usd,
            updated_at     = excluded.updated_at,
            loaded_at      = excluded.loaded_at
        """,
        (now,),
    )
    conn.execute("DROP TABLE IF EXISTS stg_orders")
    after = conn.execute("SELECT COUNT(*) FROM fact_orders").fetchone()[0]
    log.info("fact_orders: %d inserted, %d upserted in total", after - before, len(orders))
    return len(orders)


def load_fact_events_daily(conn: sqlite3.Connection, events: pd.DataFrame) -> int:
    """Full refresh of the daily aggregate (cheap, and keeps it idempotent)."""
    conn.execute("DELETE FROM fact_events_daily")
    if events.empty:
        return 0
    agg = (
        events.assign(product_sku=events["product_sku"].fillna(""))
        .groupby(["event_date", "type", "device", "product_sku"], as_index=False)
        .agg(n_events=("event_id", "count"))
        .rename(columns={"type": "event_type"})
    )
    conn.executemany(
        "INSERT INTO fact_events_daily (event_date, event_type, device, product_sku, n_events) VALUES (?,?,?,?,?)",
        agg[["event_date", "event_type", "device", "product_sku", "n_events"]].itertuples(index=False, name=None),
    )
    return len(agg)


# ------------------------------------------------------- watermark & run log
def get_watermark(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute("SELECT last_value FROM etl_watermark WHERE source = ?", (source,)).fetchone()
    return row[0] if row else None


def set_watermark(conn: sqlite3.Connection, source: str, value: str) -> None:
    conn.execute(
        """INSERT INTO etl_watermark (source, last_value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET last_value = excluded.last_value, updated_at = excluded.updated_at""",
        (source, value, utc_now_iso()),
    )


def start_run(conn: sqlite3.Connection, mode: str) -> int:
    cur = conn.execute("INSERT INTO etl_run_log (started_at, status, mode) VALUES (?, 'running', ?)", (utc_now_iso(), mode))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, extracted: int = 0, loaded: int = 0,
               rejected: int = 0, duration: float = 0.0, error: str | None = None) -> None:
    conn.execute(
        """UPDATE etl_run_log SET finished_at = ?, status = ?, rows_extracted = ?, rows_loaded = ?,
           rows_rejected = ?, duration_s = ?, error = ? WHERE run_id = ?""",
        (utc_now_iso(), status, extracted, loaded, rejected, round(duration, 3), error, run_id),
    )
    conn.commit()


def save_dq(conn: sqlite3.Connection, run_id: int, results: list) -> None:
    conn.executemany(
        "INSERT INTO dq_result (run_id, name, passed, severity, detail) VALUES (?,?,?,?,?)",
        [(run_id, r.name, int(r.passed), r.severity, r.detail) for r in results],
    )
    conn.commit()


# --------------------------------------------------------------------- query
def query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def simulate_day(offset: int = 0) -> str:
    """Helper for demos/tests: an ISO date `offset` days from today."""
    return (date.today() + timedelta(days=offset)).isoformat()
