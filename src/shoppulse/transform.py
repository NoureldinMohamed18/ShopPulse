"""Transform layer (Pandas + NumPy).

Each function takes a raw DataFrame and returns ``(clean, rejected)``.
Rejected rows are *never silently dropped*: they carry a ``reject_reason`` and
are written to the quarantine folder so someone can investigate them.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from shoppulse.config import ORDER_STATUSES, SUPPORTED_CURRENCIES

log = logging.getLogger(__name__)


def _reject(df: pd.DataFrame, mask: pd.Series, reason: str) -> pd.DataFrame:
    out = df[mask].copy()
    out["reject_reason"] = reason
    return out


def clean_orders(raw: pd.DataFrame, rates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Clean orders and convert every amount to USD.

    Steps: normalise text -> validate -> deduplicate -> convert currency -> derive columns.
    """
    if raw.empty:
        return raw.assign(total_usd=pd.Series(dtype=float)), raw.assign(reject_reason=pd.Series(dtype=str))

    df = raw.copy()
    df["status"] = df["status"].astype("string").str.strip().str.lower()
    df["currency"] = df["currency"].astype("string").str.strip().str.upper()
    df["order_date"] = pd.to_datetime(df["order_date"], format="%Y-%m-%d", errors="coerce")

    rejected: list[pd.DataFrame] = []
    checks = [
        ("invalid_or_missing_date", df["order_date"].isna()),
        ("non_positive_quantity", ~(df["quantity"] > 0)),  # also catches NaN
        ("missing_or_invalid_price", ~(df["unit_price"] > 0)),
        ("unknown_status", ~df["status"].isin(ORDER_STATUSES)),
        ("unsupported_currency", ~df["currency"].isin(SUPPORTED_CURRENCIES)),
    ]
    bad_any = pd.Series(False, index=df.index)
    for reason, mask in checks:
        mask = mask & ~bad_any  # attribute each row to its first problem only
        if mask.any():
            rejected.append(_reject(df, mask, reason))
        bad_any |= mask

    good = df[~bad_any].copy()

    # exact duplicates: the same order id delivered twice -> keep the latest version
    before = len(good)
    good = good.sort_values(["order_id", "updated_at"]).drop_duplicates("order_id", keep="last")
    dupes = before - len(good)
    if dupes:
        log.info("Dropped %d duplicate order rows", dupes)

    # currency -> USD (rates are "currency per 1 USD")
    rate_map = rates.set_index("currency")["rate_per_usd"]
    good["fx_rate"] = good["currency"].map(rate_map)
    good["unit_price_usd"] = np.round(good["unit_price"] / good["fx_rate"], 2)
    good["total_usd"] = np.round(good["unit_price_usd"] * good["quantity"], 2)
    good["order_date"] = good["order_date"].dt.date.astype(str)
    good["quantity"] = good["quantity"].astype(int)
    good["order_id"] = good["order_id"].astype(int)

    cols = ["order_id", "customer_id", "product_sku", "quantity", "unit_price", "currency",
            "unit_price_usd", "total_usd", "order_date", "status", "updated_at"]
    rej = pd.concat(rejected, ignore_index=True) if rejected else pd.DataFrame(columns=list(raw.columns) + ["reject_reason"])
    log.info("Orders: %d clean, %d rejected, %d duplicates removed", len(good), len(rej), dupes)
    return good[cols].reset_index(drop=True), rej


def clean_customers(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = raw.copy()
    df["full_name"] = df["full_name"].astype("string").str.strip().str.title()
    df["email"] = df["email"].astype("string").str.strip().str.lower()
    df["city"] = df["city"].astype("string").str.strip().str.title()
    df["country"] = df["country"].astype("string").str.strip().str.title()
    # a missing email is not fatal for analytics - we keep the customer and flag it
    df["email"] = df["email"].fillna("unknown")
    bad = df["full_name"].isna() | (df["full_name"] == "")
    rej = _reject(df, bad, "missing_name")
    df = df[~bad].drop_duplicates("customer_id", keep="last")
    return df.reset_index(drop=True), rej


def clean_products(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = raw.copy()
    df["title"] = df["title"].astype("string").str.strip()
    bad = ~(df["price_gbp"] > 0)
    rej = _reject(df, bad, "invalid_price")
    df = df[~bad].drop_duplicates("product_sku", keep="last")
    # Price bands with NumPy: cheap / mid / premium
    df["price_band"] = np.select(
        [df["price_gbp"] < 20, df["price_gbp"] < 40], ["budget", "standard"], default="premium"
    )
    df["in_stock"] = df["in_stock"].astype(int)
    return df.reset_index(drop=True), rej


def clean_events(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if raw.empty:
        return raw, raw.assign(reject_reason=pd.Series(dtype=str))
    df = raw.copy()
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    bad = df["ts"].isna() | df["customer_id"].isna()
    rej = _reject(df, bad, "bad_timestamp_or_customer")
    df = df[~bad].drop_duplicates("event_id")
    df["event_date"] = df["ts"].dt.date.astype(str)
    if "product_sku" not in df:
        df["product_sku"] = None
    return df.reset_index(drop=True), rej


def daily_event_summary(events: pd.DataFrame) -> pd.DataFrame:
    """Pivot events into one row per day with a column per event type (GroupBy + pivot_table)."""
    if events.empty:
        return pd.DataFrame(columns=["event_date"])
    pivot = events.pivot_table(index="event_date", columns="type", values="event_id", aggfunc="count", fill_value=0)
    pivot.columns = [f"n_{c}" for c in pivot.columns]
    return pivot.reset_index()
