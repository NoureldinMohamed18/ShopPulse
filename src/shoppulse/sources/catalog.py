"""Source 2 - product catalogue via web scraping (books.toscrape.com).

Course topics covered: HTTP requests, BeautifulSoup parsing, pagination,
politeness (delay + User-Agent), error handling, robots/ethics.
books.toscrape.com is a sandbox site built *for* scraping practice.

Offline mode generates a deterministic catalogue so the pipeline and the tests
run without internet.
"""

from __future__ import annotations

import logging
import random
import re
import time
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

from shoppulse.config import Settings
from shoppulse.sources.oltp import SKUS
from shoppulse.utils import retry

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "ShopPulse-Learning-Bot/1.0 (educational project)"}
_RATING = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}
_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def parse_products(html: str, page_url: str, category: str) -> tuple[list[dict], str | None]:
    """Parse one listing page -> (products, absolute URL of the next page or None).

    Pure function (no network) - which is why it is easy to unit test.
    """
    soup = BeautifulSoup(html, "lxml")
    products = []
    for card in soup.select("article.product_pod"):
        link = card.h3.a
        price_text = card.select_one("p.price_color").get_text(strip=True)
        match = _PRICE_RE.search(price_text)
        rating_cls = [c for c in card.select_one("p.star-rating")["class"] if c != "star-rating"]
        products.append(
            {
                "title": link["title"].strip(),
                "price_gbp": float(match.group(1)) if match else None,
                "rating": _RATING.get(rating_cls[0]) if rating_cls else None,
                "in_stock": "in stock" in card.select_one("p.availability").get_text().lower(),
                "category": category,
                "url": urljoin(page_url, link["href"]),
            }
        )
    nxt = soup.select_one("li.next > a")
    return products, (urljoin(page_url, nxt["href"]) if nxt else None)


def _scrape(settings: Settings, max_pages: int) -> pd.DataFrame:
    session = requests.Session()
    session.headers.update(HEADERS)

    @retry(attempts=settings.http_retries, delay=1.0, exceptions=(requests.RequestException,))
    def get(url: str) -> str:
        resp = session.get(url, timeout=settings.http_timeout)
        resp.raise_for_status()
        return resp.text

    rows: list[dict] = []
    for cat in settings.categories:
        url: str | None = f"{settings.catalog_base_url}/catalogue/category/books/{cat}/index.html"
        category = cat.rsplit("_", 1)[0].replace("-", " ").title()
        pages = 0
        while url and pages < max_pages:
            try:
                html = get(url)
            except requests.RequestException as exc:
                log.error("Skipping %s: %s", url, exc)  # one bad page must not kill the run
                break
            items, url = parse_products(html, url, category)
            rows.extend(items)
            pages += 1
            time.sleep(settings.scrape_delay)  # be polite
    return pd.DataFrame(rows)


def _offline_catalog(seed: int = 7) -> pd.DataFrame:
    rng = random.Random(seed)
    cats = ["Travel", "Mystery", "Historical Fiction", "Science"]
    rows = [
        {
            "title": f"Sample Book {i:02d}",
            "price_gbp": round(rng.uniform(10, 55), 2),
            "rating": rng.randint(1, 5),
            "in_stock": rng.random() > 0.1,
            "category": cats[i % len(cats)],
            "url": f"https://books.toscrape.com/catalogue/sample-book-{i}/index.html",
        }
        for i in range(len(SKUS))
    ]
    return pd.DataFrame(rows)


def extract_catalog(settings: Settings, offline: bool = False, max_pages: int = 3) -> pd.DataFrame:
    """Scrape the catalogue and attach an internal SKU to each product.

    Offline (or if scraping returns nothing) we fall back to a deterministic
    catalogue so downstream steps still have data - the run is flagged in logs.
    """
    df = pd.DataFrame()
    if not offline:
        df = _scrape(settings, max_pages)
    if df.empty:
        log.warning("Catalogue scrape returned no rows - using offline sample catalogue")
        df = _offline_catalog()

    df = df.drop_duplicates(subset=["url"]).reset_index(drop=True)
    # Map scraped products to the SKUs used by the OLTP orders (cycled deterministically).
    df["product_sku"] = [SKUS[i % len(SKUS)] for i in range(len(df))]
    df = df.drop_duplicates(subset=["product_sku"]).reset_index(drop=True)
    log.info("Catalogue ready: %d products", len(df))
    return df
