"""Data-quality gate.

Runs rule-based checks on the *clean* data. Each check has a severity:
``error`` stops the pipeline (bad data must not reach the warehouse),
``warn`` is recorded but allows the run to continue.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    passed: bool
    severity: str  # "error" | "warn"
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


class DataQualityError(RuntimeError):
    def __init__(self, failures: list[CheckResult]):
        self.failures = failures
        super().__init__("; ".join(f"{f.name}: {f.detail}" for f in failures))


def check_orders(orders: pd.DataFrame, customers: pd.DataFrame, products: pd.DataFrame,
                 rejected_count: int = 0, max_reject_ratio: float = 0.25) -> list[CheckResult]:
    results: list[CheckResult] = []

    def add(name: str, passed: bool, fail_detail: str, severity: str = "error", ok_detail: str = "ok") -> None:
        """``fail_detail`` is only shown when the check fails; ``ok_detail`` when it passes."""
        results.append(CheckResult(name, bool(passed), severity, ok_detail if passed else fail_detail))

    add("orders_not_empty", len(orders) > 0, "no clean orders left after cleaning", ok_detail=f"{len(orders)} rows")
    if len(orders):
        add("order_id_unique", orders["order_id"].is_unique, "duplicate order ids")
        add("no_nulls_in_keys", not orders[["order_id", "customer_id", "product_sku"]].isna().any().any(),
            "null found in a key column")
        add("totals_positive", (orders["total_usd"] > 0).all(), "non-positive total found")
        add("total_matches_qty_price",
            ((orders["unit_price_usd"] * orders["quantity"] - orders["total_usd"]).abs() < 0.05).all(),
            "total != unit_price * quantity")
        # referential integrity: every order must point to a known customer / product
        missing_c = ~orders["customer_id"].isin(customers["customer_id"])
        add("customers_exist", not missing_c.any(), f"{int(missing_c.sum())} orders with unknown customer")
        missing_p = ~orders["product_sku"].isin(products["product_sku"])
        add("products_exist", not missing_p.any(), f"{int(missing_p.sum())} orders with unknown product", "warn")
        ratio = rejected_count / max(1, rejected_count + len(orders))
        add("reject_ratio_ok", ratio <= max_reject_ratio,
            f"{ratio:.1%} rejected (limit {max_reject_ratio:.0%})", ok_detail=f"{ratio:.1%} rejected")
    return results


def enforce(results: list[CheckResult]) -> None:
    for r in results:
        level = logging.INFO if r.passed else (logging.ERROR if r.severity == "error" else logging.WARNING)
        log.log(level, "DQ %-24s %s (%s)", r.name, "PASS" if r.passed else "FAIL", r.detail)
    failures = [r for r in results if not r.passed and r.severity == "error"]
    if failures:
        raise DataQualityError(failures)
