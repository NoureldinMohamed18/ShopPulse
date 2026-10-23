"""Serving layer #3 - static analytical report (Matplotlib + Seaborn).

Produces PNG charts and a Markdown summary in ``data/reports``.
"""

from __future__ import annotations

import logging
from contextlib import closing
from pathlib import Path

import pandas as pd

from shoppulse import viz, warehouse
from shoppulse.config import Settings

log = logging.getLogger(__name__)


def _q(settings: Settings, sql: str) -> pd.DataFrame:
    with closing(warehouse.connect(settings.warehouse_db)) as conn:
        return warehouse.query(conn, sql)


def build_report(settings: Settings) -> list[Path]:
    viz.apply_matplotlib_style()
    import matplotlib.pyplot as plt
    import seaborn as sns
    from matplotlib.colors import LinearSegmentedColormap

    out = settings.reports_dir
    out.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []

    daily = _q(settings, "SELECT * FROM v_daily_sales ORDER BY order_date")
    if daily.empty:
        raise RuntimeError("no sales in the warehouse yet - run the pipeline first")
    daily["order_date"] = pd.to_datetime(daily["order_date"])

    # 1. revenue trend: raw daily (muted) + 7-day moving average (emphasis)
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(daily["order_date"], daily["revenue_usd"], color=viz.MUTED_LINE, linewidth=1.2, label="Daily revenue")
    ax.plot(daily["order_date"], daily["revenue_7d_avg"], color=viz.BLUE, label="7-day average")
    ax.set_title("Revenue per day (USD)")
    ax.set_xlabel("")
    ax.legend(loc="upper left")
    fig.autofmt_xdate()
    files.append(_save(fig, out / "01_revenue_trend.png"))

    # 2. top products - one series, one colour, sorted
    top = _q(settings, "SELECT title, revenue_usd FROM v_top_products ORDER BY revenue_usd DESC LIMIT 10")
    fig, ax = plt.subplots(figsize=(9, 4.5))
    sns.barplot(data=top, y="title", x="revenue_usd", color=viz.BLUE, ax=ax)
    ax.set_title("Top 10 products by revenue (USD)")
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.grid(axis="y", visible=False)
    files.append(_save(fig, out / "02_top_products.png"))

    # 3. category x weekday heatmap (sequential single-hue)
    heat = _q(settings, """SELECT category, day_name, SUM(total_usd) AS revenue
                           FROM v_sales_enriched WHERE status IN ('shipped','delivered')
                           GROUP BY category, day_name""")
    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    pivot = heat.pivot(index="category", columns="day_name", values="revenue").reindex(columns=order).fillna(0)
    fig, ax = plt.subplots(figsize=(9, 3.8))
    cmap = LinearSegmentedColormap.from_list("shoppulse_seq", [viz.SEQ_LOW, viz.SEQ_HIGH])
    sns.heatmap(pivot, cmap=cmap, annot=False, linewidths=2, linecolor=viz.SURFACE, cbar=False, ax=ax)
    # annotate by hand so the number flips to white on dark cells (readable on every cell)
    threshold = pivot.values.min() + 0.55 * (pivot.values.max() - pivot.values.min())
    for i, row in enumerate(pivot.values):
        for j, value in enumerate(row):
            ax.text(j + 0.5, i + 0.5, f"{value:.0f}", ha="center", va="center", fontsize=8,
                    color="#ffffff" if value > threshold else viz.INK)
    ax.set_title("Revenue by category and weekday (USD)")
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.grid(False)
    files.append(_save(fig, out / "03_category_weekday_heatmap.png"))

    # 4. customer value distribution (NTILE quartiles from SQL window function)
    cust = _q(settings, "SELECT value_quartile, COUNT(*) AS customers, ROUND(SUM(revenue_usd),0) AS revenue FROM v_customer_value GROUP BY 1 ORDER BY 1")
    fig, ax = plt.subplots(figsize=(7, 3.8))
    sns.barplot(data=cust, x="value_quartile", y="revenue", color=viz.BLUE, ax=ax)
    ax.set_title("Revenue by customer value quartile (1 = best)")
    ax.set_xlabel("Customer value quartile")
    ax.set_ylabel("Revenue (USD)")
    ax.grid(axis="x", visible=False)
    files.append(_save(fig, out / "04_customer_quartiles.png"))

    files.append(_write_summary(settings, out, daily))
    log.info("Report written: %d files in %s", len(files), out)
    return files


def _save(fig, path: Path) -> Path:
    import matplotlib.pyplot as plt

    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path


def _write_summary(settings: Settings, out: Path, daily: pd.DataFrame) -> Path:
    k = _q(settings, """SELECT COUNT(*) AS orders, ROUND(SUM(total_usd),2) AS revenue,
                               ROUND(AVG(total_usd),2) AS aov, COUNT(DISTINCT customer_id) AS customers
                        FROM v_sales_enriched WHERE status IN ('shipped','delivered')""").iloc[0]
    best = daily.loc[daily["revenue_usd"].idxmax()]
    path = out / "summary.md"
    path.write_text(
        "# ShopPulse - sales summary\n\n"
        f"- Revenue (shipped + delivered): **${k['revenue']:,.2f}** from **{int(k['orders'])}** orders\n"
        f"- Average order value: **${k['aov']:,.2f}**; active customers: **{int(k['customers'])}**\n"
        f"- Best day: **{best['order_date'].date()}** with ${best['revenue_usd']:,.2f}\n"
        f"- Period: {daily['order_date'].min().date()} to {daily['order_date'].max().date()}\n",
        encoding="utf-8",
    )
    return path
