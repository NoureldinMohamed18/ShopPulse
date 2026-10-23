"""Central configuration.

Everything is read from environment variables with safe local defaults, so the
same code runs on a laptop, in Docker and in CI without edits.
Settings are re-read on every ``get_settings()`` call, which keeps tests simple
(they just point ``SHOPPULSE_DATA_DIR`` at a temporary folder).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_CURRENCIES: tuple[str, ...] = ("USD", "EUR", "GBP", "EGP")
ORDER_STATUSES: tuple[str, ...] = ("pending", "shipped", "delivered", "cancelled")
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "travel_2",
    "mystery_3",
    "historical-fiction_4",
    "science_22",
)


@dataclass(frozen=True)
class Settings:
    env: str  # dev | test | prod
    data_dir: Path
    mongo_uri: str | None
    mongo_db: str
    catalog_base_url: str
    rates_url: str
    http_timeout: int
    http_retries: int
    scrape_delay: float
    categories: tuple[str, ...]
    api_key: str | None
    log_level: str

    # ---- derived locations -------------------------------------------------
    @property
    def oltp_db(self) -> Path:
        return self.data_dir / "oltp" / "shop_oltp.db"

    @property
    def warehouse_db(self) -> Path:
        return self.data_dir / "warehouse" / "shoppulse_dw.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / "quarantine"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def metrics_dir(self) -> Path:
        return self.data_dir / "metrics"

    @property
    def seed_state_file(self) -> Path:
        return self.data_dir / "seed_state.json"

    def ensure_dirs(self) -> None:
        for path in (
            self.oltp_db.parent,
            self.warehouse_db.parent,
            self.raw_dir,
            self.quarantine_dir,
            self.reports_dir,
            self.logs_dir,
            self.metrics_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    categories = os.getenv("SHOPPULSE_CATEGORIES")
    return Settings(
        env=os.getenv("SHOPPULSE_ENV", "dev"),
        data_dir=Path(os.getenv("SHOPPULSE_DATA_DIR", "data")).resolve(),
        mongo_uri=os.getenv("MONGO_URI") or None,
        mongo_db=os.getenv("MONGO_DB", "shoppulse"),
        catalog_base_url=os.getenv("CATALOG_BASE_URL", "https://books.toscrape.com").rstrip("/"),
        rates_url=os.getenv("RATES_URL", "https://open.er-api.com/v6/latest/USD"),
        http_timeout=int(os.getenv("HTTP_TIMEOUT", "10")),
        http_retries=int(os.getenv("HTTP_RETRIES", "3")),
        scrape_delay=float(os.getenv("SCRAPE_DELAY", "1.0")),
        categories=tuple(c.strip() for c in categories.split(",")) if categories else DEFAULT_CATEGORIES,
        api_key=os.getenv("SHOPPULSE_API_KEY") or None,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
