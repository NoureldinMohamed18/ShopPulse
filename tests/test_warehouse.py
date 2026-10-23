import sqlite3

import pandas as pd
import pytest

from shoppulse import warehouse

ORDER_COLS = ["order_id", "customer_id", "product_sku", "quantity", "unit_price", "currency",
              "unit_price_usd", "total_usd", "order_date", "status", "updated_at"]


@pytest.fixture
def conn(settings):
    c = warehouse.connect(settings.warehouse_db)
    warehouse.init_schema(c)
    yield c
    c.close()


def customers(city="Cairo"):
    return pd.DataFrame({"customer_id": [1, 2], "full_name": ["Ali", "Mona"], "email": ["a@x.com", "m@x.com"],
                         "city": [city, "Giza"], "country": ["Egypt", "Egypt"], "signup_date": ["2025-01-01"] * 2})


def order(order_id=1, customer_id=1, date="2026-01-15", status="shipped", total=20.0):
    return [order_id, customer_id, "SKU-0001", 2, total / 2, "USD", total / 2, total, date, status, f"{date}T12:00:00"]


def orders(*rows):
    return pd.DataFrame(list(rows), columns=ORDER_COLS)


def test_schema_is_created_with_dimensions_and_views(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"dim_date", "dim_customer", "dim_product", "fact_orders", "fact_events_daily",
            "etl_run_log", "v_daily_sales", "v_top_products", "v_customer_value"} <= tables
    assert conn.execute("SELECT COUNT(*) FROM dim_date").fetchone()[0] > 365 * 5
    # unknown members exist for late-arriving facts
    assert conn.execute("SELECT COUNT(*) FROM dim_customer WHERE customer_key = -1").fetchone()[0] == 1


def test_init_schema_is_idempotent(conn):
    warehouse.init_schema(conn)
    warehouse.init_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM dim_customer WHERE customer_key = -1").fetchone()[0] == 1


def test_scd2_new_unchanged_changed(conn):
    assert warehouse.load_dim_customer(conn, customers(), "2026-02-01") == {"new": 2, "changed": 0, "unchanged": 0}
    assert warehouse.load_dim_customer(conn, customers(), "2026-02-02") == {"new": 0, "changed": 0, "unchanged": 2}

    stats = warehouse.load_dim_customer(conn, customers(city="Berlin"), "2026-03-01")
    assert stats == {"new": 0, "changed": 1, "unchanged": 1}

    rows = conn.execute("SELECT city, valid_from, valid_to, is_current FROM dim_customer WHERE customer_id = 1 ORDER BY customer_key").fetchall()
    assert [tuple(r) for r in rows] == [("Cairo", "1900-01-01", "2026-03-01", 0), ("Berlin", "2026-03-01", "9999-12-31", 1)]


def test_only_one_current_row_per_business_key(conn):
    warehouse.load_dim_customer(conn, customers(), "2026-02-01")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO dim_customer (customer_id, row_hash, valid_from) VALUES (1, 'x', '2026-01-01')")


def test_fact_uses_point_in_time_dimension_key(conn):
    warehouse.load_dim_customer(conn, customers(), "2026-02-01")
    warehouse.load_dim_customer(conn, customers(city="Berlin"), "2026-03-01")
    warehouse.load_fact_orders(conn, orders(order(1, date="2026-01-15"), order(2, date="2026-03-05")))
    city = dict(conn.execute(
        "SELECT f.order_id, c.city FROM fact_orders f JOIN dim_customer c USING (customer_key)").fetchall())
    assert city == {1: "Cairo", 2: "Berlin"}  # old order keeps the OLD city


def test_late_arriving_dimension_uses_unknown_member(conn):
    warehouse.load_fact_orders(conn, orders(order(1, customer_id=999)))
    assert conn.execute("SELECT customer_key FROM fact_orders").fetchone()[0] == -1


def test_fact_load_is_idempotent_and_updates_status(conn):
    warehouse.load_dim_customer(conn, customers(), "2026-02-01")
    batch = orders(order(1, status="pending"), order(2))
    warehouse.load_fact_orders(conn, batch)
    warehouse.load_fact_orders(conn, batch)
    assert conn.execute("SELECT COUNT(*) FROM fact_orders").fetchone()[0] == 2

    warehouse.load_fact_orders(conn, orders(order(1, status="delivered")))
    assert conn.execute("SELECT status FROM fact_orders WHERE order_id = 1").fetchone()[0] == "delivered"


def test_trigger_audits_status_changes_only(conn):
    warehouse.load_fact_orders(conn, orders(order(1, status="pending")))
    warehouse.load_fact_orders(conn, orders(order(1, status="pending")))     # no change -> no audit row
    warehouse.load_fact_orders(conn, orders(order(1, status="shipped")))
    warehouse.load_fact_orders(conn, orders(order(1, status="delivered")))
    audit = [tuple(r) for r in conn.execute("SELECT old_status, new_status FROM audit_order_status ORDER BY audit_id")]
    assert audit == [("pending", "shipped"), ("shipped", "delivered")]


def test_guard_trigger_rejects_non_positive_totals(conn):
    with pytest.raises(sqlite3.DatabaseError, match="total_usd must be positive"):
        conn.execute(
            "INSERT INTO fact_orders VALUES (1, -1, -1, 20260115, 1, 1, 0, 'USD', 'shipped', '2026-01-15', 'x', 'x')")


def test_views_exclude_cancelled_and_compute_windows(conn):
    warehouse.load_dim_customer(conn, customers(), "2026-02-01")
    warehouse.load_fact_orders(conn, orders(
        order(1, date="2026-01-01", total=10), order(2, date="2026-01-02", total=30),
        order(3, date="2026-01-02", total=999, status="cancelled")))
    daily = warehouse.query(conn, "SELECT * FROM v_daily_sales ORDER BY order_date")
    assert list(daily["revenue_usd"]) == [10.0, 30.0]
    assert list(daily["running_revenue_usd"]) == [10.0, 40.0]
    assert list(daily["revenue_7d_avg"]) == [10.0, 20.0]


def test_watermark_roundtrip(conn):
    assert warehouse.get_watermark(conn, "src") is None
    warehouse.set_watermark(conn, "src", "2026-01-01T00:00:00")
    warehouse.set_watermark(conn, "src", "2026-02-01T00:00:00")
    assert warehouse.get_watermark(conn, "src") == "2026-02-01T00:00:00"


def test_fact_events_daily_full_refresh(conn):
    ev = pd.DataFrame({"event_id": ["1", "2", "3"], "type": ["page_view", "page_view", "search"],
                       "device": ["mobile"] * 3, "event_date": ["2026-01-01"] * 3,
                       "product_sku": ["SKU-1", "SKU-1", None]})
    assert warehouse.load_fact_events_daily(conn, ev) == 2
    assert warehouse.load_fact_events_daily(conn, ev) == 2  # refresh, not append
    assert conn.execute("SELECT SUM(n_events) FROM fact_events_daily").fetchone()[0] == 3
