from contextlib import closing

import pandas as pd
import pytest

from shoppulse import cli, dashboard, pipeline, report
from shoppulse.api import create_app
from shoppulse.config import get_settings


@pytest.fixture
def built(seeded):
    pipeline.run_pipeline(seeded, mode="full", offline=True)
    return seeded


@pytest.fixture
def client(built):
    return create_app(built).test_client()


# ---------------------------------------------------------------------- API
def test_health_before_pipeline_is_degraded(settings):
    r = create_app(settings).test_client().get("/health")
    assert r.status_code == 200 and r.json["status"] == "degraded"


def test_queries_before_pipeline_return_503(settings):
    assert create_app(settings).test_client().get("/api/kpis").status_code == 503


@pytest.mark.parametrize("url", ["/health", "/api/kpis", "/api/sales/daily", "/api/sales/monthly", "/api/products/top",
                                 "/api/customers/top", "/api/countries", "/api/events/funnel", "/api/runs",
                                 "/api/quality"])
def test_read_endpoints_return_json(client, url):
    r = client.get(url)
    assert r.status_code == 200
    assert r.is_json


def test_kpis_and_filters(client):
    k = client.get("/api/kpis").json
    assert k["revenue_usd"] > 0 and k["orders"] > 0 and 0 <= k["cancellation_pct"] <= 100
    assert len(client.get("/api/products/top?limit=3").json) == 3
    days = client.get("/api/sales/daily").json
    first = days[0]["order_date"]
    assert all(d["order_date"] == first for d in client.get(f"/api/sales/daily?from={first}&to={first}").json)


@pytest.mark.parametrize("url", ["/api/products/top?limit=0", "/api/products/top?limit=abc", "/api/products/top?limit=1000",
                                 "/api/sales/daily?from=yesterday", "/api/sales/daily?to=2026-13-45"])
def test_bad_query_params_are_400(client, url):
    r = client.get(url)
    assert r.status_code == 400 and "error" in r.json


def test_sql_injection_attempt_is_harmless(client):
    r = client.get("/api/products/top?category=x'%3B DROP TABLE fact_orders;--")
    assert r.status_code == 200 and r.json == []
    assert client.get("/api/kpis").json["orders"] > 0


def test_unknown_route_and_method(client):
    assert client.get("/nope").status_code == 404
    assert client.delete("/api/kpis").status_code == 405


def test_metrics_endpoint_prometheus_format(client):
    text = client.get("/metrics").get_data(as_text=True)
    assert "shoppulse_last_run_success 1" in text and "shoppulse_last_run_rows_loaded" in text


def test_post_validation(client):
    ok = {"customer_id": 1, "product_sku": "SKU-0001", "quantity": 1, "unit_price": 10}
    assert client.post("/api/orders", data="not json").status_code == 400
    assert client.post("/api/orders", json={"customer_id": 1}).status_code == 400
    assert client.post("/api/orders", json=ok | {"quantity": 0}).status_code == 400
    assert client.post("/api/orders", json=ok | {"currency": "XYZ"}).status_code == 400
    assert client.post("/api/orders", json=ok | {"quantity": "x"}).status_code == 400
    assert client.post("/api/orders", json=ok | {"customer_id": 99999}).status_code == 404


def test_api_key_protects_writes(built, monkeypatch):
    monkeypatch.setenv("SHOPPULSE_API_KEY", "secret")
    c = create_app(get_settings()).test_client()
    body = {"customer_id": 1, "product_sku": "SKU-0001", "quantity": 1, "unit_price": 10}
    assert c.post("/api/orders", json=body).status_code == 401
    assert c.post("/api/orders", json=body, headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.post("/api/orders", json=body, headers={"X-API-Key": "secret"}).status_code == 201
    assert c.get("/api/kpis").status_code == 200  # reads stay open


def test_order_roundtrip_api_to_oltp_to_warehouse_with_audit(built, client):
    created = client.post("/api/orders", json={"customer_id": 1, "product_sku": "SKU-0001", "quantity": 2,
                                               "unit_price": 15.5, "currency": "usd"})
    assert created.status_code == 201
    oid = created.json["order_id"]
    pipeline.run_pipeline(built, mode="incremental", offline=True)

    assert client.patch(f"/api/orders/{oid}/status", json={"status": "bogus"}).status_code == 400
    assert client.patch("/api/orders/99999999/status", json={"status": "shipped"}).status_code == 404
    assert client.patch(f"/api/orders/{oid}/status", json={"status": "delivered"}).status_code == 200
    pipeline.run_pipeline(built, mode="incremental", offline=True)

    history = client.get(f"/api/orders/{oid}/history").json
    assert [(h["old_status"], h["new_status"]) for h in history] == [("pending", "delivered")]


# ------------------------------------------------------- dashboard & report
def test_dashboard_figure_builders():
    sales = pd.DataFrame({
        "order_date": ["2026-01-01", "2026-01-02", "2026-01-02"], "status": ["shipped", "delivered", "cancelled"],
        "total_usd": [10.0, 20.0, 500.0], "customer_id": [1, 2, 3], "title": ["A", "B", "C"], "country": ["Egypt"] * 3,
    })
    k = dashboard.kpi_values(sales)
    assert k == {"revenue": 30.0, "orders": 2, "aov": 15.0, "customers": 2}
    assert len(dashboard.revenue_figure(sales).data) == 2
    assert list(dashboard.top_products_figure(sales).data[0].y) == ["A", "B"]
    assert dashboard.country_figure(sales).data[0].y[0] == 30.0
    funnel = pd.DataFrame({"event_type": ["page_view", "purchase"], "events": [100, 5]})
    assert list(dashboard.funnel_figure(funnel).data[0].y) == [100, 0, 5]


def test_dashboard_app_builds_and_callback_runs(built):
    app = dashboard.create_dash_app(built)
    assert app.layout is not None
    assert any("g-rev" in key for key in app.callback_map)


def test_report_creates_charts_and_summary(built):
    files = report.build_report(built)
    names = {f.name for f in files}
    assert {"01_revenue_trend.png", "02_top_products.png", "03_category_weekday_heatmap.png",
            "04_customer_quartiles.png", "summary.md"} <= names
    assert all(f.stat().st_size > 1000 for f in files if f.suffix == ".png")
    assert "Revenue" in (built.reports_dir / "summary.md").read_text()


def test_report_without_data_gives_clear_error(settings):
    from shoppulse import warehouse

    with warehouse.session(settings.warehouse_db) as c:
        warehouse.init_schema(c)
    with pytest.raises(RuntimeError, match="run the pipeline"):
        report.build_report(settings)


# ---------------------------------------------------------------------- CLI
def test_cli_demo_status_and_sql(settings, capsys):
    assert cli.main(["demo"]) == 0
    assert cli.main(["status"]) == 0
    assert cli.main(["sql", "list"]) == 0
    out = capsys.readouterr().out
    assert "top3_products_per_category" in out and "success" in out
    assert cli.main(["sql", "top3_products_per_category"]) == 0
    assert cli.main(["sql", "does_not_exist"]) == 2


def test_cli_analytics_queries_all_execute(built):
    import sqlite3

    with closing(sqlite3.connect(built.warehouse_db)) as conn:
        for sql in cli._analytics_queries().values():
            conn.execute(sql).fetchall()  # raises if any query is broken
    assert len(cli._analytics_queries()) >= 6


def test_cli_seed_and_run(settings):
    assert cli.main(["seed", "--days", "3", "--orders-per-day", "10", "--events", "50", "--clean"]) == 0
    assert cli.main(["run", "--mode", "full", "--offline"]) == 0


# --------------------------------------------------------------- monitoring
def test_monitoring_report_and_endpoint(built, client):
    from shoppulse import monitoring

    rep = monitoring.collect(built)
    assert {"operational", "data_quality", "resources", "business", "env"} <= rep.keys()
    assert rep["operational"]["last_run_status"] == "success" and rep["data_quality"]["failed"] == []
    assert rep["resources"]["peak_memory_mb"] > 0
    assert rep["status"] in {"ok", "warn"}
    assert client.get("/api/monitoring").json["env"] == "dev"
    assert client.get("/health").json["env"] == "dev"


def test_monitoring_before_pipeline_is_critical(settings, capsys):
    from shoppulse import monitoring

    assert monitoring.collect(settings)["status"] == "critical"
    assert cli.main(["monitor"]) == 1


def test_monitoring_flags_a_stale_pipeline(built):
    from datetime import datetime, timedelta, timezone

    from shoppulse import monitoring

    rep = monitoring.collect(built, now=datetime.now(timezone.utc) + timedelta(hours=48))
    assert rep["operational"]["status"] == "warn" and rep["status"] == "warn"


def test_business_check_detects_revenue_collapse(settings):
    from contextlib import closing

    from shoppulse import monitoring, warehouse

    with closing(warehouse.connect(settings.warehouse_db)) as conn:
        warehouse.init_schema(conn)
        rows = [[i, 1, "SKU-0001", 1, 100.0, "USD", 100.0, 100.0, f"2026-01-{i:02d}", "delivered", f"2026-01-{i:02d}T00:00:00"]
                for i in range(1, 9)]
        rows.append([9, 1, "SKU-0001", 1, 10.0, "USD", 10.0, 10.0, "2026-01-09", "delivered", "2026-01-09T00:00:00"])
        warehouse.load_fact_orders(conn, pd.DataFrame(rows, columns=[
            "order_id", "customer_id", "product_sku", "quantity", "unit_price", "currency", "unit_price_usd",
            "total_usd", "order_date", "status", "updated_at"]))
        conn.commit()
        result = monitoring.business(conn)
    assert result["status"] == "warn" and result["drop_pct"] == 90.0


def test_cli_run_reports_quality_failure_cleanly(seeded, monkeypatch, capsys):
    from shoppulse import transform

    monkeypatch.setattr(transform, "clean_customers", lambda raw: (raw.iloc[0:0], raw.iloc[0:0].assign(reject_reason="x")))
    assert cli.main(["run", "--mode", "full", "--offline"]) == 1
    err = capsys.readouterr().err
    assert "data-quality gate" in err and "customers_exist" in err
