"""Serving layer #2 - interactive dashboard (Plotly Dash).

Figure builders are plain functions that take DataFrames, so they can be unit
tested without starting a web server. The Dash callback only wires a date-range
filter and a category dropdown to those functions.
"""

from __future__ import annotations

from contextlib import closing

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, dash_table, dcc, html

from shoppulse import viz, warehouse
from shoppulse.config import Settings, get_settings


# ---------------------------------------------------------------- data access
def _load(settings: Settings, sql: str, params: tuple = ()) -> pd.DataFrame:
    with closing(warehouse.connect(settings.warehouse_db)) as conn:
        return warehouse.query(conn, sql, params)


# ------------------------------------------------------------ figure builders
def kpi_values(sales: pd.DataFrame) -> dict:
    ok = sales[sales["status"].isin(["shipped", "delivered"])]
    return {
        "revenue": float(ok["total_usd"].sum()),
        "orders": int(len(ok)),
        "aov": float(ok["total_usd"].mean()) if len(ok) else 0.0,
        "customers": int(ok["customer_id"].nunique()),
    }


def revenue_figure(sales: pd.DataFrame) -> go.Figure:
    ok = sales[sales["status"].isin(["shipped", "delivered"])]
    daily = ok.groupby("order_date", as_index=False)["total_usd"].sum().sort_values("order_date")
    daily["avg7"] = daily["total_usd"].rolling(7, min_periods=1).mean()
    fig = go.Figure()
    fig.add_scatter(x=daily["order_date"], y=daily["total_usd"], name="Daily revenue",
                    line=dict(color=viz.MUTED_LINE, width=1.5), hovertemplate="$%{y:,.0f}")
    fig.add_scatter(x=daily["order_date"], y=daily["avg7"], name="7-day average",
                    line=dict(color=viz.BLUE, width=2.5), hovertemplate="$%{y:,.0f}")
    fig.update_layout(**viz.plotly_layout("Revenue per day (USD)"))
    return fig


def top_products_figure(sales: pd.DataFrame, limit: int = 8) -> go.Figure:
    ok = sales[sales["status"].isin(["shipped", "delivered"])]
    top = ok.groupby("title", as_index=False)["total_usd"].sum().nlargest(limit, "total_usd").sort_values("total_usd")
    fig = go.Figure(go.Bar(x=top["total_usd"], y=top["title"], orientation="h", marker_color=viz.BLUE,
                           hovertemplate="%{y}: $%{x:,.0f}<extra></extra>"))
    fig.update_layout(**viz.plotly_layout("Top products by revenue (USD)") | {"hovermode": "closest"})
    fig.update_yaxes(showgrid=False)
    return fig


def country_figure(sales: pd.DataFrame) -> go.Figure:
    ok = sales[sales["status"].isin(["shipped", "delivered"])]
    by = ok.groupby("country", as_index=False)["total_usd"].sum().sort_values("total_usd", ascending=False)
    fig = go.Figure(go.Bar(x=by["country"], y=by["total_usd"], marker_color=viz.BLUE,
                           hovertemplate="%{x}: $%{y:,.0f}<extra></extra>"))
    fig.update_layout(**viz.plotly_layout("Revenue by country (USD)") | {"hovermode": "closest"})
    fig.update_xaxes(showgrid=False)
    return fig


def funnel_figure(funnel: pd.DataFrame) -> go.Figure:
    order = ["page_view", "add_to_cart", "purchase"]
    f = funnel[funnel["event_type"].isin(order)].set_index("event_type").reindex(order).fillna(0).reset_index()
    fig = go.Figure(go.Bar(x=f["event_type"], y=f["events"], marker_color=viz.BLUE,
                           hovertemplate="%{x}: %{y:,.0f}<extra></extra>"))
    fig.update_layout(**viz.plotly_layout("Browse-to-buy funnel (events)") | {"hovermode": "closest"})
    fig.update_xaxes(showgrid=False)
    return fig


# ------------------------------------------------------------------ the app
def _card(label: str, value_id: str) -> html.Div:
    return html.Div(
        [html.Div(label, style={"color": viz.INK_2, "fontSize": 13}),
         html.Div(id=value_id, style={"fontSize": 28, "fontWeight": 600, "color": viz.INK})],
        style={"background": viz.SURFACE, "border": f"1px solid {viz.GRID}", "borderRadius": 8,
               "padding": "12px 16px", "flex": "1 1 160px"},
    )


def create_dash_app(settings: Settings | None = None) -> Dash:
    cfg = settings or get_settings()
    app = Dash(__name__, title="ShopPulse")
    sales0 = _load(cfg, "SELECT * FROM v_sales_enriched")
    categories = sorted(sales0["category"].dropna().unique())
    dmin, dmax = (sales0["order_date"].min(), sales0["order_date"].max()) if len(sales0) else (None, None)

    app.layout = html.Div(
        style={"background": viz.SURFACE, "color": viz.INK, "fontFamily": "system-ui, sans-serif",
               "padding": 16, "maxWidth": 1200, "margin": "0 auto"},
        children=[
            html.H2("ShopPulse - sales dashboard", style={"marginBottom": 4}),
            html.Div("Data comes from the warehouse views, refreshed by the pipeline.", style={"color": viz.INK_2}),
            html.Div(
                [dcc.DatePickerRange(id="dates", start_date=dmin, end_date=dmax, display_format="YYYY-MM-DD"),
                 dcc.Dropdown(id="category", options=[{"label": c, "value": c} for c in categories],
                              multi=True, placeholder="All categories", style={"minWidth": 260})],
                style={"display": "flex", "gap": 12, "flexWrap": "wrap", "margin": "16px 0"},
            ),
            html.Div([_card("Revenue", "k-rev"), _card("Orders", "k-orders"),
                      _card("Avg order", "k-aov"), _card("Customers", "k-cust")],
                     style={"display": "flex", "gap": 12, "flexWrap": "wrap", "marginBottom": 12}),
            dcc.Graph(id="g-rev"),
            html.Div([dcc.Graph(id="g-top", style={"flex": "1 1 420px"}),
                      dcc.Graph(id="g-country", style={"flex": "1 1 320px"})],
                     style={"display": "flex", "flexWrap": "wrap", "gap": 12}),
            dcc.Graph(id="g-funnel"),
            html.H3("Recent pipeline runs"),
            html.Div(id="runs"),
        ],
    )

    @app.callback(
        Output("k-rev", "children"), Output("k-orders", "children"), Output("k-aov", "children"),
        Output("k-cust", "children"), Output("g-rev", "figure"), Output("g-top", "figure"),
        Output("g-country", "figure"), Output("g-funnel", "figure"), Output("runs", "children"),
        Input("dates", "start_date"), Input("dates", "end_date"), Input("category", "value"),
    )
    def refresh(start, end, cats):
        sales = _load(cfg, "SELECT * FROM v_sales_enriched")
        if start:
            sales = sales[sales["order_date"] >= str(start)[:10]]
        if end:
            sales = sales[sales["order_date"] <= str(end)[:10]]
        if cats:
            sales = sales[sales["category"].isin(cats)]
        k = kpi_values(sales)
        funnel = _load(cfg, "SELECT event_type, SUM(n_events) AS events FROM fact_events_daily GROUP BY 1")
        runs = _load(cfg, "SELECT run_id, status, mode, rows_extracted, rows_loaded, rows_rejected, duration_s, finished_at "
                          "FROM etl_run_log ORDER BY run_id DESC LIMIT 8")
        table = dash_table.DataTable(
            data=runs.to_dict("records"), columns=[{"name": c, "id": c} for c in runs.columns],
            style_cell={"fontSize": 12, "padding": 6, "color": viz.INK, "backgroundColor": viz.SURFACE},
            style_header={"fontWeight": 600},
        )
        return (f"${k['revenue']:,.0f}", f"{k['orders']:,}", f"${k['aov']:,.2f}", f"{k['customers']:,}",
                revenue_figure(sales), top_products_figure(sales), country_figure(sales),
                funnel_figure(funnel), table)

    return app
