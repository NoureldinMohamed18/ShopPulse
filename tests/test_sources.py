import json

import pytest
import requests

from shoppulse.config import get_settings
from shoppulse.sources import catalog, events, oltp, rates

PAGE_1 = """
<html><body>
<article class="product_pod"><h3><a href="a-book_1/index.html" title="A Book">A Bo...</a></h3>
  <p class="star-rating Three"></p><p class="price_color">£23.88</p>
  <p class="availability"> In stock </p></article>
<article class="product_pod"><h3><a href="b-book_2/index.html" title="B Book">B Bo...</a></h3>
  <p class="star-rating Five"></p><p class="price_color">Â£51.77</p>
  <p class="availability">Out of stock</p></article>
<ul class="pager"><li class="next"><a href="page-2.html">next</a></li></ul>
</body></html>"""
PAGE_2 = """<article class="product_pod"><h3><a href="c_3/index.html" title="C Book">C</a></h3>
  <p class="star-rating One"></p><p class="price_color">£9.99</p><p class="availability">In stock</p></article>"""


# --------------------------------------------------------------------- OLTP
def test_seed_oltp_is_deterministic_and_continues(settings):
    first = oltp.seed_oltp(settings, days=5, orders_per_day=10)
    again = oltp.seed_oltp(settings, days=5, orders_per_day=10)
    assert first["next_day"] == 5 and again["next_day"] == 10
    orders = oltp.extract_orders(settings)
    assert orders["order_id"].max() >= 100  # 10 days x 10 orders
    assert orders["order_date"].nunique() >= 10


def test_oltp_contains_injected_dirt(settings):
    oltp.seed_oltp(settings, days=30, orders_per_day=25)
    df = oltp.extract_orders(settings)
    assert df["order_id"].duplicated().any()
    assert (df["quantity"] < 0).any()
    assert df["unit_price"].isna().any()


def test_incremental_extract_respects_watermark(seeded):
    all_rows = oltp.extract_orders(seeded)
    cut = sorted(all_rows["updated_at"].unique())[4]
    newer = oltp.extract_orders(seeded, since=cut)
    assert 0 < len(newer) < len(all_rows)
    assert (newer["updated_at"] >= cut).all()


# ------------------------------------------------------------------ catalog
def test_parse_products_extracts_fields_and_next_link():
    products, nxt = catalog.parse_products(PAGE_1, "https://x.test/cat/index.html", "Travel")
    assert [p["title"] for p in products] == ["A Book", "B Book"]
    assert products[0]["price_gbp"] == 23.88 and products[1]["price_gbp"] == 51.77  # survives mojibake
    assert [p["rating"] for p in products] == [3, 5]
    assert [p["in_stock"] for p in products] == [True, False]
    assert products[0]["url"] == "https://x.test/cat/a-book_1/index.html"
    assert nxt == "https://x.test/cat/page-2.html"


def test_parse_products_last_page_has_no_next():
    _, nxt = catalog.parse_products(PAGE_2, "https://x.test/cat/page-2.html", "Travel")
    assert nxt is None


def test_scrape_follows_pagination(monkeypatch):
    pages = {"https://books.toscrape.com/catalogue/category/books/travel_2/index.html": PAGE_1,
             "https://books.toscrape.com/catalogue/category/books/travel_2/page-2.html": PAGE_2}

    class FakeResp:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass

    class FakeSession:
        headers = {}
        def get(self, url, timeout=None):
            return FakeResp(pages[url])

    monkeypatch.setattr(catalog.requests, "Session", lambda: FakeSession())
    monkeypatch.setenv("SHOPPULSE_CATEGORIES", "travel_2")
    df = catalog._scrape(get_settings(), max_pages=5)
    assert len(df) == 3 and set(df["category"]) == {"Travel"}


def test_scrape_survives_a_failing_page(monkeypatch):
    class FakeSession:
        headers = {}
        def get(self, url, timeout=None):
            raise requests.ConnectionError("down")

    monkeypatch.setattr(catalog.requests, "Session", lambda: FakeSession())
    assert catalog._scrape(get_settings(), max_pages=2).empty


def test_extract_catalog_offline_assigns_unique_skus(settings):
    df = catalog.extract_catalog(settings, offline=True)
    assert df["product_sku"].is_unique and len(df) == len(oltp.SKUS)


# -------------------------------------------------------------------- rates
class Resp:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code, self._payload, self._bad = status, payload, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


OK_PAYLOAD = {"rates": {"USD": 1, "EUR": 0.9, "GBP": 0.8, "EGP": 50, "JPY": 150}}


def test_rates_success_and_cache(settings, monkeypatch):
    monkeypatch.setattr(rates.requests, "get", lambda *a, **k: Resp(payload=OK_PAYLOAD))
    df = rates.extract_rates(settings)
    assert set(df["currency"]) == {"USD", "EUR", "GBP", "EGP"}
    assert json.loads((settings.raw_dir / "rates_latest.json").read_text())["EGP"] == 50


def test_rates_retries_on_503_then_succeeds(settings, monkeypatch):
    seq = iter([Resp(503), Resp(503), Resp(payload=OK_PAYLOAD)])
    monkeypatch.setattr(rates.requests, "get", lambda *a, **k: next(seq))
    assert len(rates.extract_rates(settings)) == 4


def test_rates_falls_back_to_cache_then_static(settings, monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(rates.requests, "get", boom)
    static = rates.extract_rates(settings)  # no cache yet -> built-in
    assert static.set_index("currency").loc["EGP", "rate_per_usd"] == rates.FALLBACK_RATES["EGP"]
    (settings.raw_dir / "rates_latest.json").write_text(json.dumps({"USD": 1, "EUR": 2, "GBP": 3, "EGP": 4}))
    cached = rates.extract_rates(settings)
    assert cached.set_index("currency").loc["EUR", "rate_per_usd"] == 2


@pytest.mark.parametrize("payload", [{"rates": {"USD": 1}}, {"rates": {"USD": 1, "EUR": -1, "GBP": 1, "EGP": 1}}, {"nope": 1}])
def test_parse_rates_rejects_bad_payloads(payload):
    with pytest.raises(rates.RatesError):
        rates.parse_rates(payload.get("rates", payload))


def test_rates_invalid_json_is_handled(settings, monkeypatch):
    monkeypatch.setattr(rates.requests, "get", lambda *a, **k: Resp(bad_json=True))
    df = rates.extract_rates(settings)  # falls back instead of crashing
    assert len(df) == 4


# ------------------------------------------------------------------- events
def test_events_seed_extract_and_funnel(settings):
    assert events.seed_events(settings, n=500) == 500
    df = events.extract_events(settings)
    assert len(df) == 500 and df["event_id"].is_unique
    assert "_id" not in df.columns
    f = events.funnel(settings)
    assert f["events"].sum() == 500
    assert events.seed_events(settings, n=100) == 100  # idempotent: replaces, not appends
    assert len(events.extract_events(settings)) == 100
