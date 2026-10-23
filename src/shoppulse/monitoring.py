"""Pipeline monitoring across the four dimensions from the course:

* operational      - did the runs succeed, how long do they take, how stale is the data?
* data quality     - did the last run's quality checks pass?
* resources        - memory, disk
* business logic   - e.g. a sudden drop in revenue (the pipeline can be "green" and still wrong)

``collect()`` returns a plain dict, used by the API (/api/monitoring) and the CLI (``monitor``).
"""

from __future__ import annotations

try:
    import resource
except ImportError:
    resource = None
import shutil
from contextlib import closing
from datetime import datetime, timezone

from shoppulse import warehouse
from shoppulse.config import Settings

REVENUE_DROP_ALERT_PCT = 40.0   # alert if the latest day is >40% below the previous 7-day average
STALE_AFTER_HOURS = 26          # a daily pipeline that has not succeeded for >26h is late


def operational(conn, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    runs = [dict(r) for r in conn.execute("SELECT * FROM etl_run_log ORDER BY run_id DESC LIMIT 10")]
    if not runs:
        return {"status": "critical", "detail": "pipeline has never run"}
    ok = [r for r in runs if r["status"] == "success"]
    last_ok = next((r for r in runs if r["status"] == "success" and r["finished_at"]), None)
    hours = None
    if last_ok:
        finished = datetime.fromisoformat(last_ok["finished_at"]).replace(tzinfo=timezone.utc)
        hours = round((now - finished).total_seconds() / 3600, 1)
    status = "ok"
    if runs[0]["status"] == "failed":
        status = "critical"
    elif hours is None or hours > STALE_AFTER_HOURS:
        status = "warn"
    durations = [r["duration_s"] for r in ok if r["duration_s"] is not None]
    return {
        "status": status,
        "last_run_status": runs[0]["status"],
        "success_rate_last_10": round(len(ok) / len(runs), 2),
        "avg_duration_s": round(sum(durations) / len(durations), 2) if durations else None,
        "hours_since_last_success": hours,
    }


def data_quality(conn) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT name, passed, severity FROM dq_result WHERE run_id = (SELECT MAX(run_id) FROM dq_result)")]
    if not rows:
        return {"status": "warn", "detail": "no quality results recorded yet"}
    failed = [r for r in rows if not r["passed"]]
    hard = [r for r in failed if r["severity"] == "error"]
    return {"status": "critical" if hard else ("warn" if failed else "ok"),
            "checks": len(rows), "failed": [r["name"] for r in failed]}


def resources(settings: Settings) -> dict:
    peak_kb = (
    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if resource is not None
    else 1024
    )
    data_bytes = sum(f.stat().st_size for f in settings.data_dir.rglob("*") if f.is_file())
    free = shutil.disk_usage(settings.data_dir).free
    free_mb = round(free / 1e6)
    return {"status": "warn" if free_mb < 500 else "ok", "peak_memory_mb": round(peak_kb / 1024, 1),
            "data_dir_mb": round(data_bytes / 1e6, 1), "disk_free_mb": free_mb}


def business(conn) -> dict:
    days = [r["revenue_usd"] for r in conn.execute("SELECT revenue_usd FROM v_daily_sales ORDER BY order_date")]
    if len(days) < 4:
        return {"status": "ok", "detail": "not enough history for a revenue-drop check"}
    last, previous = days[-1], days[-8:-1]
    baseline = sum(previous) / len(previous)
    drop = round(100 * (baseline - last) / baseline, 1) if baseline else 0.0
    return {"status": "warn" if drop > REVENUE_DROP_ALERT_PCT else "ok", "latest_day_revenue": last,
            "baseline_7d_avg": round(baseline, 2), "drop_pct": max(drop, 0.0)}


def collect(settings: Settings, now: datetime | None = None) -> dict:
    if not settings.warehouse_db.exists():
        return {"status": "critical", "env": settings.env, "detail": "warehouse not built yet"}
    with closing(warehouse.connect(settings.warehouse_db)) as conn:
        dims = {"operational": operational(conn, now), "data_quality": data_quality(conn),
                "resources": resources(settings), "business": business(conn)}
    order = {"ok": 0, "warn": 1, "critical": 2}
    overall = max((d["status"] for d in dims.values()), key=order.__getitem__)
    return {"status": overall, "env": settings.env, **dims}
