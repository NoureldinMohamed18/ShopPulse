"""Airflow DAG - the same three stages, orchestrated.

    start_run -> extract -> transform -> load -> build_report -> finish_run

This maps 1:1 onto the stages in ``shoppulse.pipeline``: because the stages talk
through files (data/raw, data/staging), every Airflow task can run in its own
process and be retried independently.

Airflow itself is NOT a dependency of the package (it is heavy); install it
with the docker-compose file in this repo (``docker compose --profile airflow up``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from airflow.decorators import dag, task

from shoppulse import pipeline, warehouse
from shoppulse.config import get_settings

default_args = {
    "owner": "data-eng",
    "retries": 2,                         # transient failures (network, locked DB) retry automatically
    "retry_delay": timedelta(minutes=2),
}


@dag(
    dag_id="shoppulse_daily",
    description="Daily ShopPulse ETL: 4 sources -> warehouse -> report",
    schedule="0 2 * * *",                 # every day at 02:00
    start_date=datetime(2026, 1, 1),
    catchup=False,                        # don't back-fill every missed day on first start
    max_active_runs=1,                    # never two runs writing to the warehouse at once
    default_args=default_args,
    tags=["shoppulse", "etl"],
)
def shoppulse_daily():
    @task
    def start_run() -> int:
        settings = get_settings()
        settings.ensure_dirs()
        with warehouse.session(settings.warehouse_db) as conn:
            warehouse.init_schema(conn)
            return warehouse.start_run(conn, "incremental")

    @task
    def extract() -> dict:
        return pipeline.stage_extract(get_settings(), mode="incremental")

    @task
    def transform(run_id: int, _extract: dict) -> dict:
        res = pipeline.stage_transform(get_settings(), run_id=run_id)
        res.pop("dq_results")             # keep XCom small & JSON-serialisable
        return res

    @task
    def load(_transform: dict) -> dict:
        return pipeline.stage_load(get_settings())

    @task
    def build_report(_load: dict) -> None:
        from shoppulse.report import build_report as build

        build(get_settings())

    @task(trigger_rule="all_done")
    def finish_run(run_id: int, _report: None = None) -> None:
        # runs even on failure so the run log never stays in 'running'
        from airflow.operators.python import get_current_context

        ctx = get_current_context()
        failed = any(ti.state == "failed" for ti in ctx["dag_run"].get_task_instances())
        settings = get_settings()
        with warehouse.session(settings.warehouse_db) as conn:
            warehouse.finish_run(conn, run_id, "failed" if failed else "success")

    rid = start_run()
    ext = extract()
    tr = transform(rid, ext)
    ld = load(tr)
    rep = build_report(ld)
    finish_run(rid, rep)


shoppulse_daily()
