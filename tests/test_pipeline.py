import json
import sqlite3
from contextlib import closing

import pytest

from shoppulse import pipeline, quality, transform, warehouse
from shoppulse.sources import oltp


def scalar(settings, sql):
    with closing(warehouse.connect(settings.warehouse_db)) as c:
        return c.execute(sql).fetchone()[0]


def test_full_run_loads_everything(seeded):
    result = pipeline.run_pipeline(seeded, mode="full", offline=True)
    assert result["status"] == "success"
    assert scalar(seeded, "SELECT COUNT(*) FROM fact_orders") > 100
    assert scalar(seeded, "SELECT COUNT(*) FROM dim_customer WHERE customer_id > 0") == 200
    assert scalar(seeded, "SELECT COUNT(*) FROM dim_product WHERE product_sku <> 'UNKNOWN'") == 40
    assert scalar(seeded, "SELECT COUNT(*) FROM fact_events_daily") > 0
    assert scalar(seeded, "SELECT status FROM etl_run_log ORDER BY run_id DESC LIMIT 1") == "success"
    assert scalar(seeded, "SELECT COUNT(*) FROM dq_result") >= 8
    assert json.loads((seeded.metrics_dir / "last_run.json").read_text())["status"] == "success"


def test_bad_rows_land_in_quarantine_not_in_the_warehouse(seeded):
    result = pipeline.run_pipeline(seeded, mode="full", offline=True)
    assert result["rejected"] > 0
    files = list(seeded.quarantine_dir.glob("orders_rejected_*.csv"))
    assert files and "reject_reason" in files[0].read_text().splitlines()[0]
    assert scalar(seeded, "SELECT COUNT(*) FROM fact_orders WHERE total_usd <= 0 OR quantity <= 0") == 0


def test_rerunning_is_idempotent(seeded):
    pipeline.run_pipeline(seeded, mode="full", offline=True)
    before = (scalar(seeded, "SELECT COUNT(*) FROM fact_orders"),
              scalar(seeded, "SELECT COUNT(*) FROM dim_customer"),
              scalar(seeded, "SELECT ROUND(SUM(total_usd), 2) FROM fact_orders"))
    pipeline.run_pipeline(seeded, mode="full", offline=True)
    pipeline.run_pipeline(seeded, mode="incremental", offline=True)
    after = (scalar(seeded, "SELECT COUNT(*) FROM fact_orders"),
             scalar(seeded, "SELECT COUNT(*) FROM dim_customer"),
             scalar(seeded, "SELECT ROUND(SUM(total_usd), 2) FROM fact_orders"))
    assert before == after


def test_incremental_run_only_extracts_new_rows(seeded):
    pipeline.run_pipeline(seeded, mode="full", offline=True)
    total_before = scalar(seeded, "SELECT COUNT(*) FROM fact_orders")
    oltp.seed_oltp(seeded, days=5, orders_per_day=20)          # the shop keeps selling

    result = pipeline.run_pipeline(seeded, mode="incremental", offline=True)
    all_rows = len(oltp.extract_orders(seeded))
    assert result["extracted"]["orders"] < all_rows            # not a full reload
    assert scalar(seeded, "SELECT COUNT(*) FROM fact_orders") > total_before


def test_incremental_picks_up_scd2_change_with_effective_date(seeded):
    pipeline.run_pipeline(seeded, mode="full", offline=True, as_of="2026-02-01")
    with closing(oltp.connect(seeded.oltp_db)) as c:
        c.execute("UPDATE customers SET city = 'atlantis' WHERE customer_id = 1")
        c.commit()
    result = pipeline.run_pipeline(seeded, mode="incremental", offline=True, as_of="2026-03-01")
    assert result["loaded"]["dim_customer"]["changed"] == 1
    with closing(warehouse.connect(seeded.warehouse_db)) as c:
        rows = c.execute("SELECT city, valid_to FROM dim_customer WHERE customer_id = 1 ORDER BY customer_key").fetchall()
    assert rows[-1]["city"] == "Atlantis" and rows[0]["valid_to"] == "2026-03-01"


def test_data_quality_failure_stops_the_load_and_is_logged(seeded, monkeypatch):
    def no_customers(raw):
        return raw.iloc[0:0], raw.iloc[0:0].assign(reject_reason="x")

    monkeypatch.setattr(transform, "clean_customers", no_customers)
    with pytest.raises(quality.DataQualityError):
        pipeline.run_pipeline(seeded, mode="full", offline=True)

    assert scalar(seeded, "SELECT COUNT(*) FROM fact_orders") == 0          # nothing reached the warehouse
    assert scalar(seeded, "SELECT status FROM etl_run_log ORDER BY run_id DESC LIMIT 1") == "failed"
    assert scalar(seeded, "SELECT COUNT(*) FROM dq_result WHERE passed = 0") >= 1
    assert json.loads((seeded.metrics_dir / "last_run.json").read_text())["status"] == "failed"


def test_load_failure_rolls_back_everything(seeded, monkeypatch):
    def explode(conn, df):
        raise sqlite3.OperationalError("disk full")

    pipeline.run_pipeline(seeded, mode="full", offline=True)
    count = scalar(seeded, "SELECT COUNT(*) FROM dim_customer")
    oltp.seed_oltp(seeded, days=3, orders_per_day=10)
    with closing(oltp.connect(seeded.oltp_db)) as c:
        c.execute("UPDATE customers SET city = 'moved' WHERE customer_id = 2")
        c.commit()
    monkeypatch.setattr(warehouse, "load_fact_orders", explode)
    with pytest.raises(sqlite3.OperationalError):
        pipeline.run_pipeline(seeded, mode="incremental", offline=True, as_of="2026-04-01")
    # the dimension change made *before* the failing step was rolled back too
    assert scalar(seeded, "SELECT COUNT(*) FROM dim_customer") == count
    assert scalar(seeded, "SELECT COUNT(*) FROM dim_customer WHERE city = 'Moved'") == 0


def test_stages_can_run_separately(seeded):
    pipeline.stage_extract(seeded, mode="full", offline=True)
    assert (seeded.raw_dir / "orders.csv").exists()
    pipeline.stage_transform(seeded)
    assert (seeded.data_dir / "staging" / "orders_clean.csv").exists()
    out = pipeline.stage_load(seeded)
    assert out["orders_loaded"] > 0
