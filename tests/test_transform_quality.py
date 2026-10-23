import pandas as pd
import pytest

from shoppulse import quality, transform

RATES = pd.DataFrame({"currency": ["USD", "EUR", "EGP", "GBP"], "rate_per_usd": [1.0, 0.5, 50.0, 0.8],
                      "rate_date": "2026-01-01"})


def orders(rows):
    cols = ["order_id", "customer_id", "product_sku", "quantity", "unit_price", "currency",
            "order_date", "status", "updated_at"]
    return pd.DataFrame(rows, columns=cols)


GOOD = [1, 10, "SKU-0001", 2, 10.0, "USD", "2026-01-02", "delivered", "2026-01-02T12:00:00"]


def test_clean_orders_converts_currency_and_totals():
    egp = [2, 11, "SKU-0002", 3, 100.0, "EGP", "2026-01-03", "shipped", "2026-01-03T12:00:00"]
    clean, rej = transform.clean_orders(orders([GOOD, egp]), RATES)
    assert rej.empty
    by_id = clean.set_index("order_id")
    assert by_id.loc[1, "total_usd"] == 20.0
    assert by_id.loc[2, "unit_price_usd"] == 2.0 and by_id.loc[2, "total_usd"] == 6.0


@pytest.mark.parametrize(
    "mutation, reason",
    [
        ({"quantity": -1}, "non_positive_quantity"),
        ({"unit_price": None}, "missing_or_invalid_price"),
        ({"order_date": "31/02/2026"}, "invalid_or_missing_date"),
        ({"status": "teleported"}, "unknown_status"),
        ({"currency": "XYZ"}, "unsupported_currency"),
    ],
)
def test_bad_rows_are_quarantined_with_a_reason(mutation, reason):
    bad = dict(zip(orders([GOOD]).columns, GOOD, strict=True)) | mutation
    bad["order_id"] = 99
    clean, rej = transform.clean_orders(orders([GOOD, list(bad.values())]), RATES)
    assert list(clean["order_id"]) == [1]
    assert list(rej["reject_reason"]) == [reason]


def test_status_case_and_whitespace_are_normalised():
    row = GOOD.copy()
    row[7] = "  DELIVERED "
    clean, rej = transform.clean_orders(orders([row]), RATES)
    assert rej.empty and clean.loc[0, "status"] == "delivered"


def test_duplicate_order_ids_keep_latest():
    older = GOOD.copy()
    newer = GOOD.copy()
    older[7], older[8] = "pending", "2026-01-02T08:00:00"
    clean, _ = transform.clean_orders(orders([newer, older]), RATES)
    assert len(clean) == 1 and clean.loc[0, "status"] == "delivered"


def test_clean_customers_fills_missing_email_and_titles_names():
    raw = pd.DataFrame({"customer_id": [1, 2], "full_name": [" ali hassan ", "mona"], "email": [None, "A@B.com"],
                        "city": ["cairo", "giza"], "country": ["egypt", "egypt"], "signup_date": ["2025-01-01"] * 2})
    clean, rej = transform.clean_customers(raw)
    assert rej.empty
    assert clean.loc[0, "full_name"] == "Ali Hassan" and clean.loc[0, "email"] == "unknown"
    assert clean.loc[1, "email"] == "a@b.com"


def test_clean_products_price_bands():
    raw = pd.DataFrame({"product_sku": ["a", "b", "c", "d"], "title": ["A", "B", "C", "D"],
                        "price_gbp": [10.0, 25.0, 50.0, -1.0], "in_stock": [True, True, False, True],
                        "rating": [1, 2, 3, 4], "category": ["x"] * 4, "url": ["u"] * 4})
    clean, rej = transform.clean_products(raw)
    assert dict(zip(clean["product_sku"], clean["price_band"], strict=True)) == {"a": "budget", "b": "standard", "c": "premium"}
    assert list(rej["product_sku"]) == ["d"]


def test_daily_event_summary_pivots_types():
    ev = pd.DataFrame({"event_id": ["1", "2", "3"], "type": ["page_view", "page_view", "purchase"],
                       "event_date": ["2026-01-01"] * 3})
    out = transform.daily_event_summary(ev).iloc[0]
    assert out["n_page_view"] == 2 and out["n_purchase"] == 1


# ------------------------------------------------------------------ quality
def _frames():
    clean, _ = transform.clean_orders(orders([GOOD]), RATES)
    cust = pd.DataFrame({"customer_id": [10]})
    prod = pd.DataFrame({"product_sku": ["SKU-0001"]})
    return clean, cust, prod


def test_quality_passes_on_good_data():
    clean, cust, prod = _frames()
    results = quality.check_orders(clean, cust, prod)
    assert all(r.passed for r in results)
    quality.enforce(results)  # no exception


def test_quality_blocks_unknown_customer():
    clean, _, prod = _frames()
    results = quality.check_orders(clean, pd.DataFrame({"customer_id": [999]}), prod)
    with pytest.raises(quality.DataQualityError) as exc:
        quality.enforce(results)
    assert "customers_exist" in str(exc.value)


def test_quality_unknown_product_only_warns():
    clean, cust, _ = _frames()
    results = quality.check_orders(clean, cust, pd.DataFrame({"product_sku": ["other"]}))
    quality.enforce(results)  # warn-level: does not raise
    assert any(r.name == "products_exist" and not r.passed for r in results)


def test_quality_fails_when_too_many_rows_rejected():
    clean, cust, prod = _frames()
    results = quality.check_orders(clean, cust, prod, rejected_count=100)
    with pytest.raises(quality.DataQualityError):
        quality.enforce(results)
