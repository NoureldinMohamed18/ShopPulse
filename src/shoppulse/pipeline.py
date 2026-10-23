"""Pipeline orchestration.

The pipeline is split into three independent *stages* that talk to each other
through files, never through memory:

    extract  -> data/raw/*.csv
    transform-> data/staging/*.csv   (+ data/quarantine/*.csv for rejected rows)
    load     -> warehouse (SQLite)

Why files between stages? Each stage can be re-run alone, inspected by hand,
and mapped 1:1 onto an Airflow task (see dags/shoppulse_dag.py). ``run_pipeline``
simply calls the three stages in order - it is what the CLI and the tests use.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from pathlib import Path

import pandas as pd

from shoppulse import quality, transform, warehouse
from shoppulse.config import Settings
from shoppulse.sources import catalog, events, oltp, rates
from shoppulse.utils import utc_now_iso

log = logging.getLogger(__name__)
ORDERS_SOURCE = "oltp.orders"


def _staging(settings: Settings) -> Path:
    path = settings.data_dir / "staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------------ stage 1
def stage_extract(settings: Settings, mode: str = "incremental", offline: bool = False) -> dict:
    """Pull from all four sources and land the data *as is* in data/raw."""
    settings.ensure_dirs()
    if not settings.oltp_db.exists():
        oltp.seed_oltp(settings)
    if offline or not settings.mongo_uri:
        # no real MongoDB around -> make sure the in-memory one has data
        if events.get_collection(settings).count_documents({}) == 0:
            events.seed_events(settings)

    with warehouse.session(settings.warehouse_db) as conn:
        warehouse.init_schema(conn)
        since = warehouse.get_watermark(conn, ORDERS_SOURCE) if mode == "incremental" else None

    raw = settings.raw_dir
    data = {
        "orders": oltp.extract_orders(settings, since=since),
        "customers": oltp.extract_customers(settings),
        "products": catalog.extract_catalog(settings, offline=offline),
        "events": events.extract_events(settings),
        "rates": rates.extract_rates(settings, offline=offline),
    }
    for name, df in data.items():
        df.to_csv(raw / f"{name}.csv", index=False)

    counts = {name: len(df) for name, df in data.items()}
    log.info("EXTRACT done: %s", counts)
    return {"counts": counts, "since": since}


# ------------------------------------------------------------------ stage 2
def stage_transform(settings: Settings, run_id: int | None = None) -> dict:
    """Clean raw files, quarantine bad rows, run the data-quality gate."""
    raw, stg = settings.raw_dir, _staging(settings)
    orders_raw = pd.read_csv(raw / "orders.csv")
    customers_raw = pd.read_csv(raw / "customers.csv")
    products_raw = pd.read_csv(raw / "products.csv")
    events_raw = pd.read_csv(raw / "events.csv")
    fx = pd.read_csv(raw / "rates.csv")

    orders, orders_rej = transform.clean_orders(orders_raw, fx)
    customers, customers_rej = transform.clean_customers(customers_raw)
    products, products_rej = transform.clean_products(products_raw)
    evts, events_rej = transform.clean_events(events_raw)

    tag = f"run{run_id}" if run_id is not None else time.strftime("%Y%m%d_%H%M%S")
    rejected_total = 0
    for name, rej in [("orders", orders_rej), ("customers", customers_rej),
                      ("products", products_rej), ("events", events_rej)]:
        rejected_total += len(rej)
        if len(rej):
            rej.to_csv(settings.quarantine_dir / f"{name}_rejected_{tag}.csv", index=False)

    # DQ gate (only meaningful when there are new orders to judge)
    results: list[quality.CheckResult] = []
    if len(orders_raw):
        results = quality.check_orders(orders, customers, products, rejected_count=len(orders_rej))
    (settings.metrics_dir / "dq_last.json").write_text(json.dumps([r.as_dict() for r in results], indent=2))
    quality.enforce(results)  # raises DataQualityError -> nothing reaches the warehouse

    for name, df in [("orders", orders), ("customers", customers), ("products", products), ("events", evts)]:
        df.to_csv(stg / f"{name}_clean.csv", index=False)
    summary = {"orders_clean": len(orders), "rejected": rejected_total, "dq_checks": len(results)}
    log.info("TRANSFORM done: %s", summary)
    return summary | {"dq_results": results}


# ------------------------------------------------------------------ stage 3
def stage_load(settings: Settings, as_of: str | None = None) -> dict:
    """Load clean files into the warehouse in ONE transaction (all or nothing)."""
    stg = _staging(settings)
    orders = pd.read_csv(stg / "orders_clean.csv")
    customers = pd.read_csv(stg / "customers_clean.csv")
    products = pd.read_csv(stg / "products_clean.csv")
    evts = pd.read_csv(stg / "events_clean.csv")

    conn = warehouse.connect(settings.warehouse_db)
    try:
        warehouse.init_schema(conn)
        cust_stats = warehouse.load_dim_customer(conn, customers, as_of)
        prod_stats = warehouse.load_dim_product(conn, products, as_of)
        loaded = warehouse.load_fact_orders(conn, orders)
        n_events = warehouse.load_fact_events_daily(conn, evts)
        if len(orders):
            warehouse.set_watermark(conn, ORDERS_SOURCE, str(orders["updated_at"].max()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    summary = {"orders_loaded": loaded, "event_rows": n_events, "dim_customer": cust_stats, "dim_product": prod_stats}
    log.info("LOAD done: %s", summary)
    return summary


# ------------------------------------------------------------------ all-in-one
def run_pipeline(settings: Settings, mode: str = "incremental", offline: bool = False,
                 as_of: str | None = None) -> dict:
    """Run extract -> transform -> load with run logging and metrics."""
    settings.ensure_dirs()
    with warehouse.session(settings.warehouse_db) as conn:
        warehouse.init_schema(conn)
        run_id = warehouse.start_run(conn, mode)

    started = time.perf_counter()
    log.info("=== ShopPulse run %d started (mode=%s, offline=%s) ===", run_id, mode, offline)
    try:
        ex = stage_extract(settings, mode=mode, offline=offline)
        tr = stage_transform(settings, run_id=run_id)
        ld = stage_load(settings, as_of=as_of)
    except Exception as exc:
        duration = time.perf_counter() - started
        log.error("Run %d FAILED: %s", run_id, exc)
        with warehouse.session(settings.warehouse_db) as conn:
            if isinstance(exc, quality.DataQualityError):
                warehouse.save_dq(conn, run_id, exc.failures)
            warehouse.finish_run(conn, run_id, "failed", duration=duration, error=f"{type(exc).__name__}: {exc}"[:500])
        _write_metrics(settings, {"run_id": run_id, "status": "failed", "error": str(exc),
                                  "traceback": traceback.format_exc(limit=3), "finished_at": utc_now_iso()})
        raise

    duration = time.perf_counter() - started
    with warehouse.session(settings.warehouse_db) as conn:
        warehouse.save_dq(conn, run_id, tr["dq_results"])
        warehouse.finish_run(conn, run_id, "success", extracted=ex["counts"]["orders"],
                             loaded=ld["orders_loaded"], rejected=tr["rejected"], duration=duration)
    result = {
        "run_id": run_id, "status": "success", "mode": mode, "duration_s": round(duration, 2),
        "extracted": ex["counts"], "rejected": tr["rejected"], "loaded": ld, "finished_at": utc_now_iso(),
    }
    _write_metrics(settings, result)
    log.info("=== Run %d finished OK in %.2fs ===", run_id, duration)
    return result


def _write_metrics(settings: Settings, payload: dict) -> None:
    settings.metrics_dir.mkdir(parents=True, exist_ok=True)
    (settings.metrics_dir / "last_run.json").write_text(json.dumps(payload, indent=2, default=str))
