-- ShopPulse Data Warehouse - STAR SCHEMA (SQLite)
--
--            dim_date
--               |
--  dim_customer-+-fact_orders-+-dim_product
--  (SCD Type 2)               (SCD Type 2)
--               fact_events_daily
--
-- Surrogate keys (customer_key, product_key) are used instead of business keys so
-- that a customer who moves city can have TWO rows (history preserved = SCD2).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS dim_date (
    date_key     INTEGER PRIMARY KEY,          -- 20260131
    full_date    TEXT NOT NULL UNIQUE,         -- 2026-01-31
    year         INTEGER NOT NULL,
    quarter      INTEGER NOT NULL,
    month        INTEGER NOT NULL,
    month_name   TEXT NOT NULL,
    day          INTEGER NOT NULL,
    day_of_week  INTEGER NOT NULL,             -- 0 = Monday
    day_name     TEXT NOT NULL,
    is_weekend   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_customer (
    customer_key INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  INTEGER NOT NULL,             -- business key
    full_name    TEXT,
    email        TEXT,
    city         TEXT,
    country      TEXT,
    signup_date  TEXT,
    row_hash     TEXT NOT NULL,                -- fingerprint of tracked attributes
    valid_from   TEXT NOT NULL,
    valid_to     TEXT NOT NULL DEFAULT '9999-12-31',
    is_current   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_dim_customer_bk ON dim_customer (customer_id, is_current);
-- at most ONE current version per business key
CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_customer_current ON dim_customer (customer_id) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS dim_product (
    product_key  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_sku  TEXT NOT NULL,                -- business key
    title        TEXT,
    category     TEXT,
    price_gbp    REAL,
    price_band   TEXT,
    rating       INTEGER,
    in_stock     INTEGER,
    url          TEXT,
    row_hash     TEXT NOT NULL,
    valid_from   TEXT NOT NULL,
    valid_to     TEXT NOT NULL DEFAULT '9999-12-31',
    is_current   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_dim_product_bk ON dim_product (product_sku, is_current);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_product_current ON dim_product (product_sku) WHERE is_current = 1;

-- "Unknown member" rows: facts that arrive before their dimension row still load.
INSERT OR IGNORE INTO dim_customer (customer_key, customer_id, full_name, email, city, country, row_hash, valid_from)
VALUES (-1, -1, 'Unknown', 'unknown', 'Unknown', 'Unknown', 'unknown', '1900-01-01');
INSERT OR IGNORE INTO dim_product (product_key, product_sku, title, category, row_hash, valid_from)
VALUES (-1, 'UNKNOWN', 'Unknown', 'Unknown', 'unknown', '1900-01-01');

CREATE TABLE IF NOT EXISTS fact_orders (
    order_id       INTEGER PRIMARY KEY,        -- grain: one row per order
    customer_key   INTEGER NOT NULL REFERENCES dim_customer (customer_key),
    product_key    INTEGER NOT NULL REFERENCES dim_product (product_key),
    date_key       INTEGER NOT NULL REFERENCES dim_date (date_key),
    quantity       INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_usd REAL NOT NULL,
    total_usd      REAL NOT NULL,
    currency       TEXT NOT NULL,
    status         TEXT NOT NULL,
    order_date     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    loaded_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fact_orders_date     ON fact_orders (date_key);
CREATE INDEX IF NOT EXISTS ix_fact_orders_customer ON fact_orders (customer_key);
CREATE INDEX IF NOT EXISTS ix_fact_orders_product  ON fact_orders (product_key);
CREATE INDEX IF NOT EXISTS ix_fact_orders_status   ON fact_orders (status);

CREATE TABLE IF NOT EXISTS fact_events_daily (
    event_date   TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    device       TEXT NOT NULL,
    product_sku  TEXT NOT NULL DEFAULT '',
    n_events     INTEGER NOT NULL,
    PRIMARY KEY (event_date, event_type, device, product_sku)
);

-- ---------------------------------------------------------------- operations
CREATE TABLE IF NOT EXISTS etl_watermark (
    source     TEXT PRIMARY KEY,
    last_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS etl_run_log (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,               -- running | success | failed
    mode          TEXT,
    rows_extracted INTEGER DEFAULT 0,
    rows_loaded    INTEGER DEFAULT 0,
    rows_rejected  INTEGER DEFAULT 0,
    duration_s    REAL,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS dq_result (
    run_id   INTEGER NOT NULL,
    name     TEXT NOT NULL,
    passed   INTEGER NOT NULL,
    severity TEXT NOT NULL,
    detail   TEXT
);

CREATE TABLE IF NOT EXISTS audit_order_status (
    audit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL,
    old_status TEXT,
    new_status TEXT,
    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------------ triggers
-- 1) Audit trail: every status change of an order is recorded automatically.
CREATE TRIGGER IF NOT EXISTS trg_fact_orders_status_audit
AFTER UPDATE OF status ON fact_orders
WHEN OLD.status <> NEW.status
BEGIN
    INSERT INTO audit_order_status (order_id, old_status, new_status)
    VALUES (OLD.order_id, OLD.status, NEW.status);
END;

-- 2) Guard rail: the warehouse refuses nonsense amounts even if a bug upstream lets one through.
CREATE TRIGGER IF NOT EXISTS trg_fact_orders_guard
BEFORE INSERT ON fact_orders
WHEN NEW.total_usd <= 0
BEGIN
    SELECT RAISE(ABORT, 'fact_orders: total_usd must be positive');
END;
