import pytest

from shoppulse.config import Settings, get_settings
from shoppulse.sources import events, oltp


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Every test gets its own data folder, no real MongoDB, no waiting."""
    monkeypatch.setenv("SHOPPULSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("MONGO_URI", raising=False)
    monkeypatch.delenv("SHOPPULSE_API_KEY", raising=False)
    monkeypatch.setenv("SCRAPE_DELAY", "0")
    monkeypatch.setenv("HTTP_RETRIES", "3")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    events._mock_client = None  # fresh in-memory MongoDB per test


@pytest.fixture
def settings() -> Settings:
    s = get_settings()
    s.ensure_dirs()
    return s


@pytest.fixture
def seeded(settings) -> Settings:
    oltp.seed_oltp(settings, days=10, orders_per_day=20)
    events.seed_events(settings, n=300)
    return settings
