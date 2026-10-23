"""Serving layer #1 - Flask REST API on top of the warehouse.

Read endpoints (GET) query the reporting *views*; write endpoints (POST/PATCH)
talk to the OLTP database - exactly like the real shop would - and the next
incremental pipeline run carries those changes into the warehouse.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timezone

from flask import Flask, jsonify, request

from shoppulse import monitoring, warehouse
from shoppulse.config import ORDER_STATUSES, SUPPORTED_CURRENCIES, Settings, get_settings
from shoppulse.sources import oltp

log = logging.getLogger(__name__)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def create_app(settings: Settings | None = None) -> Flask:
    cfg = settings or get_settings()
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False

    # ----------------------------------------------------------- helpers
    def wh_rows(sql: str, params: tuple = ()) -> list[dict]:
        if not cfg.warehouse_db.exists():
            raise ApiError("warehouse not built yet - run the pipeline first", 503)
        with closing(warehouse.connect(cfg.warehouse_db)) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def int_arg(name: str, default: int, lo: int = 1, hi: int = 100) -> int:
        raw = request.args.get(name, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ApiError(f"'{name}' must be an integer") from None
        if not lo <= value <= hi:
            raise ApiError(f"'{name}' must be between {lo} and {hi}")
        return value

    def date_arg(name: str) -> str | None:
        raw = request.args.get(name)
        if raw is None:
            return None
        if not _DATE_RE.match(raw):
            raise ApiError(f"'{name}' must look like YYYY-MM-DD")
        try:
            date.fromisoformat(raw)
        except ValueError:
            raise ApiError(f"'{name}' is not a real date") from None
        return raw

    def require_key() -> None:
        if cfg.api_key and request.headers.get("X-API-Key") != cfg.api_key:
            raise ApiError("missing or invalid X-API-Key", 401)

    # ----------------------------------------------------------- errors
    @app.errorhandler(ApiError)
    def handle_api_error(err: ApiError):
        return jsonify(error=err.message), err.status

    @app.errorhandler(404)
    def handle_404(_):
        return jsonify(error="not found"), 404

    @app.errorhandler(405)
    def handle_405(_):
        return jsonify(error="method not allowed"), 405

    @app.errorhandler(sqlite3.Error)
    def handle_db(err: sqlite3.Error):
        log.exception("database error")
        return jsonify(error="database error"), 500

    # ------------------------------------------------------------- GET
    @app.get("/health")
    def health():
        built = cfg.warehouse_db.exists()
        last = wh_rows("SELECT run_id, status, finished_at FROM etl_run_log ORDER BY run_id DESC LIMIT 1") if built else []
        return jsonify(status="ok" if built else "degraded", env=cfg.env, warehouse_built=built,
                       last_run=last[0] if last else None)

    @app.get("/api/kpis")
    def kpis():
        row = wh_rows(
            """SELECT COUNT(*) AS orders,
                      COALESCE(ROUND(SUM(total_usd), 2), 0) AS revenue_usd,
                      COALESCE(ROUND(AVG(total_usd), 2), 0) AS avg_order_usd,
                      COUNT(DISTINCT customer_id) AS customers
               FROM v_sales_enriched WHERE status IN ('shipped', 'delivered')"""
        )[0]
        rate = wh_rows("SELECT ROUND(100.0 * SUM(status = 'cancelled') / NULLIF(COUNT(*), 0), 1) AS pct FROM fact_orders")[0]
        return jsonify(row | {"cancellation_pct": rate["pct"] or 0})

    @app.get("/api/sales/daily")
    def sales_daily():
        start, end = date_arg("from"), date_arg("to")
        sql, params = "SELECT * FROM v_daily_sales WHERE 1=1", []
        if start:
            sql += " AND order_date >= ?"
            params.append(start)
        if end:
            sql += " AND order_date <= ?"
            params.append(end)
        return jsonify(wh_rows(sql + " ORDER BY order_date", tuple(params)))

    @app.get("/api/sales/monthly")
    def sales_monthly():
        return jsonify(wh_rows("SELECT * FROM v_monthly_growth ORDER BY month"))

    @app.get("/api/products/top")
    def top_products():
        limit = int_arg("limit", 10)
        category = request.args.get("category")
        sql, params = "SELECT * FROM v_top_products", []
        if category:
            sql += " WHERE category = ?"
            params.append(category)
        sql += " ORDER BY revenue_usd DESC LIMIT ?"
        params.append(limit)
        return jsonify(wh_rows(sql, tuple(params)))

    @app.get("/api/customers/top")
    def top_customers():
        return jsonify(wh_rows("SELECT * FROM v_customer_value ORDER BY revenue_usd DESC LIMIT ?", (int_arg("limit", 10),)))

    @app.get("/api/countries")
    def countries():
        return jsonify(wh_rows("SELECT * FROM v_country_sales ORDER BY revenue_usd DESC"))

    @app.get("/api/events/funnel")
    def funnel():
        return jsonify(wh_rows(
            """SELECT event_type, SUM(n_events) AS events FROM fact_events_daily
               GROUP BY event_type ORDER BY events DESC"""))

    @app.get("/api/runs")
    def runs():
        return jsonify(wh_rows("SELECT * FROM etl_run_log ORDER BY run_id DESC LIMIT ?", (int_arg("limit", 10, hi=200),)))

    @app.get("/api/quality")
    def quality_latest():
        return jsonify(wh_rows(
            """SELECT d.run_id, d.name, d.passed, d.severity, d.detail FROM dq_result d
               WHERE d.run_id = (SELECT MAX(run_id) FROM dq_result) ORDER BY d.rowid"""))

    @app.get("/api/orders/<int:order_id>/history")
    def order_history(order_id: int):
        return jsonify(wh_rows("SELECT * FROM audit_order_status WHERE order_id = ? ORDER BY audit_id", (order_id,)))

    @app.get("/api/monitoring")
    def monitoring_report():
        """Operational + data-quality + resource + business-logic health in one document."""
        return jsonify(monitoring.collect(cfg))

    @app.get("/metrics")
    def metrics():
        """Prometheus text format - the hook for external monitoring/alerting."""
        last = wh_rows("SELECT * FROM etl_run_log ORDER BY run_id DESC LIMIT 1")
        lines = ["# HELP shoppulse_last_run_success 1 if the latest pipeline run succeeded",
                 "# TYPE shoppulse_last_run_success gauge"]
        if last:
            r = last[0]
            age = 0.0
            if r["finished_at"]:
                finished = datetime.fromisoformat(r["finished_at"]).replace(tzinfo=timezone.utc)
                age = max(0.0, time.time() - finished.timestamp())
            lines += [f"shoppulse_last_run_success {int(r['status'] == 'success')}",
                      f"shoppulse_last_run_rows_loaded {r['rows_loaded'] or 0}",
                      f"shoppulse_last_run_rows_rejected {r['rows_rejected'] or 0}",
                      f"shoppulse_last_run_duration_seconds {r['duration_s'] or 0}",
                      f"shoppulse_last_run_age_seconds {age:.0f}"]
        else:
            lines.append("shoppulse_last_run_success 0")
        return "\n".join(lines) + "\n", 200, {"Content-Type": "text/plain; version=0.0.4"}

    # ------------------------------------------------- writes (to the OLTP)
    @app.post("/api/orders")
    def create_order():
        require_key()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ApiError("body must be a JSON object")
        missing = [k for k in ("customer_id", "product_sku", "quantity", "unit_price") if k not in body]
        if missing:
            raise ApiError(f"missing fields: {', '.join(missing)}")
        try:
            customer_id, qty = int(body["customer_id"]), int(body["quantity"])
            price = float(body["unit_price"])
        except (TypeError, ValueError):
            raise ApiError("customer_id/quantity must be integers and unit_price a number") from None
        currency = str(body.get("currency", "USD")).upper()
        if qty <= 0 or price <= 0:
            raise ApiError("quantity and unit_price must be positive")
        if currency not in SUPPORTED_CURRENCIES:
            raise ApiError(f"currency must be one of {', '.join(SUPPORTED_CURRENCIES)}")
        sku = str(body["product_sku"]).strip()
        if not sku:
            raise ApiError("product_sku is empty")

        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with closing(oltp.connect(cfg.oltp_db)) as conn:
            exists = conn.execute("SELECT 1 FROM customers WHERE customer_id = ?", (customer_id,)).fetchone()
            if not exists:
                raise ApiError(f"customer {customer_id} does not exist", 404)
            order_id = conn.execute("SELECT COALESCE(MAX(order_id), 0) + 1 FROM orders").fetchone()[0]
            conn.execute(
                "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)",
                (order_id, customer_id, sku, qty, price, currency, date.today().isoformat(), "pending", now),
            )
            conn.commit()
        return jsonify(order_id=order_id, status="pending"), 201

    @app.patch("/api/orders/<int:order_id>/status")
    def update_status(order_id: int):
        require_key()
        body = request.get_json(silent=True) or {}
        status = str(body.get("status", "")).lower()
        if status not in ORDER_STATUSES:
            raise ApiError(f"status must be one of {', '.join(ORDER_STATUSES)}")
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with closing(oltp.connect(cfg.oltp_db)) as conn:
            cur = conn.execute("UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?", (status, now, order_id))
            conn.commit()
        if cur.rowcount == 0:
            raise ApiError(f"order {order_id} not found", 404)
        return jsonify(order_id=order_id, status=status)

    return app
