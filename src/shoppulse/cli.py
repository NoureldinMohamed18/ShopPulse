"""Command line interface:  python -m shoppulse <command>

    demo        seed everything + run the full pipeline + build the report (offline, 1 command)
    seed        (re)generate / extend the simulated OLTP and MongoDB sources
    run         run the pipeline (incremental by default)
    report      build the Seaborn report
    api         start the Flask REST API
    dashboard   start the Dash dashboard
    sql         run a query from the analytics cookbook
    status      show last runs and data-quality results
    monitor     health report (JSON) across the four monitoring dimensions
"""

from __future__ import annotations

import argparse
import re
import sys
from contextlib import closing

import pandas as pd

from shoppulse import __version__, warehouse
from shoppulse.config import get_settings
from shoppulse.logging_setup import configure_logging


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shoppulse", description="ShopPulse data platform")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="seed + full run + report, fully offline")

    s = sub.add_parser("seed", help="generate simulated source data")
    s.add_argument("--days", type=int, default=30, help="days of orders to add")
    s.add_argument("--orders-per-day", type=int, default=25)
    s.add_argument("--events", type=int, default=2000)
    s.add_argument("--clean", action="store_true", help="do not inject dirty rows")

    r = sub.add_parser("run", help="run the pipeline")
    r.add_argument("--mode", choices=["full", "incremental"], default="incremental")
    r.add_argument("--offline", action="store_true", help="no scraping / live API")
    r.add_argument("--as-of", help="effective date for SCD2 changes (YYYY-MM-DD)")

    sub.add_parser("report", help="build charts + summary")

    for name in ("api", "dashboard"):
        sp = sub.add_parser(name)
        sp.add_argument("--host", default="127.0.0.1")
        sp.add_argument("--port", type=int, default=5000 if name == "api" else 8050)

    q = sub.add_parser("sql", help="run an analytics query by name, or 'list'")
    q.add_argument("name")

    sub.add_parser("status", help="last runs and data-quality results")
    sub.add_parser("monitor", help="health across operational / quality / resources / business")
    return p


def _analytics_queries() -> dict[str, str]:
    text = (warehouse.SQL_DIR / "analytics.sql").read_text(encoding="utf-8")
    parts = re.split(r"^-- name:\s*(\S+)\s*$", text, flags=re.M)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    settings.ensure_dirs()
    configure_logging(settings.logs_dir, settings.log_level)

    # imported lazily so `--help` stays instant
    if args.command == "seed":
        from shoppulse.sources import events, oltp

        oltp.seed_oltp(settings, days=args.days, orders_per_day=args.orders_per_day, dirty=not args.clean)
        events.seed_events(settings, n=args.events)
        return 0

    if args.command == "run":
        from shoppulse.pipeline import run_pipeline
        from shoppulse.quality import DataQualityError

        try:
            run_pipeline(settings, mode=args.mode, offline=args.offline, as_of=args.as_of)
        except DataQualityError as exc:
            # an expected, explainable failure: show the reason, not a traceback
            print("\nPipeline stopped by the data-quality gate - nothing was loaded:", file=sys.stderr)
            for failure in exc.failures:
                print(f"  - {failure.name}: {failure.detail}", file=sys.stderr)
            print("Failed checks and run history: python -m shoppulse status", file=sys.stderr)
            return 1
        return 0

    if args.command == "report":
        from shoppulse.report import build_report

        for path in build_report(settings):
            print(path)
        return 0

    if args.command == "demo":
        from shoppulse.pipeline import run_pipeline
        from shoppulse.report import build_report
        from shoppulse.sources import events, oltp

        oltp.seed_oltp(settings)
        events.seed_events(settings)
        run_pipeline(settings, mode="full", offline=True)
        for path in build_report(settings):
            print(path)
        print("\nNext:  python -m shoppulse api   |   python -m shoppulse dashboard   |   python -m shoppulse status")
        return 0

    if args.command == "api":
        from shoppulse.api import create_app

        create_app(settings).run(host=args.host, port=args.port)
        return 0

    if args.command == "dashboard":
        from shoppulse.dashboard import create_dash_app

        create_dash_app(settings).run(host=args.host, port=args.port, debug=False)
        return 0

    if args.command == "sql":
        queries = _analytics_queries()
        if args.name == "list":
            print("\n".join(queries))
            return 0
        if args.name not in queries:
            print(f"unknown query '{args.name}'. available: {', '.join(queries)}", file=sys.stderr)
            return 2
        with closing(warehouse.connect(settings.warehouse_db)) as conn:
            print(warehouse.query(conn, queries[args.name]).to_string(index=False))
        return 0

    if args.command == "status":
        with closing(warehouse.connect(settings.warehouse_db)) as conn:
            pd.set_option("display.width", 200)
            print(warehouse.query(conn, "SELECT run_id, status, mode, rows_extracted, rows_loaded, rows_rejected, duration_s, finished_at FROM etl_run_log ORDER BY run_id DESC LIMIT 5").to_string(index=False))
            print()
            print(warehouse.query(conn, "SELECT name, passed, severity, detail FROM dq_result WHERE run_id = (SELECT MAX(run_id) FROM dq_result)").to_string(index=False))
        return 0

    if args.command == "monitor":
        import json

        from shoppulse import monitoring

        report = monitoring.collect(settings)
        print(json.dumps(report, indent=2))
        return 0 if report["status"] != "critical" else 1

    return 1
