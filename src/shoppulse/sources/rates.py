"""Source 3 - currency exchange rates from a public REST API.

Handles the failure modes the course talks about: timeouts, HTTP error codes
(4xx/5xx), malformed JSON. Retries with backoff, then falls back to the last
cached copy, then to built-in static rates so the pipeline never dies because
of a third-party API.
"""

from __future__ import annotations

import json
import logging
from datetime import date

import pandas as pd
import requests

from shoppulse.config import SUPPORTED_CURRENCIES, Settings
from shoppulse.utils import retry

log = logging.getLogger(__name__)

# Used only when both the live API and the cache are unavailable.
FALLBACK_RATES = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79, "EGP": 48.0}


class RatesError(RuntimeError):
    pass


def _fetch(settings: Settings) -> dict:
    @retry(attempts=settings.http_retries, delay=1.0, exceptions=(requests.RequestException, RatesError))
    def call() -> dict:
        resp = requests.get(settings.rates_url, timeout=settings.http_timeout)
        if resp.status_code >= 500:
            raise RatesError(f"server error {resp.status_code}")  # retryable
        resp.raise_for_status()  # 4xx -> RequestException (also retried, then raised)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RatesError("response is not valid JSON") from exc
        rates = payload.get("rates")
        if not isinstance(rates, dict):
            raise RatesError("payload has no 'rates' object")
        return rates

    return call()


def parse_rates(rates: dict, rate_date: str | None = None) -> pd.DataFrame:
    rows = []
    for cur in SUPPORTED_CURRENCIES:
        if cur not in rates:
            raise RatesError(f"currency {cur} missing from API response")
        value = float(rates[cur])
        if value <= 0:
            raise RatesError(f"non-positive rate for {cur}: {value}")
        rows.append({"rate_date": rate_date or date.today().isoformat(), "currency": cur, "rate_per_usd": value})
    return pd.DataFrame(rows)


def extract_rates(settings: Settings, offline: bool = False) -> pd.DataFrame:
    """Return a DataFrame(rate_date, currency, rate_per_usd) - 1 USD = X currency."""
    cache = settings.raw_dir / "rates_latest.json"
    settings.raw_dir.mkdir(parents=True, exist_ok=True)

    if not offline:
        try:
            rates = _fetch(settings)
            cache.write_text(json.dumps(rates))
            log.info("Exchange rates fetched from API")
            return parse_rates(rates)
        except (requests.RequestException, RatesError) as exc:
            log.warning("Live rates unavailable (%s) - trying cache", exc)

    if cache.exists():
        log.info("Using cached exchange rates")
        return parse_rates(json.loads(cache.read_text()))

    log.warning("Using built-in fallback exchange rates")
    return parse_rates(FALLBACK_RATES)
