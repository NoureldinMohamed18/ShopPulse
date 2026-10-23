"""Source 4 - user behaviour events in MongoDB (NoSQL).

Events are semi-structured JSON (different event types carry different fields) -
the classic reason to use a document store.

Real MongoDB is used when ``MONGO_URI`` is set (Docker Compose does this).
Otherwise ``mongomock`` provides an in-memory MongoDB-compatible engine, so the
same PyMongo code runs in tests and on a laptop with nothing installed.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta

import pandas as pd

from shoppulse.config import Settings
from shoppulse.sources.oltp import SKUS

log = logging.getLogger(__name__)

EVENT_TYPES = ["page_view", "add_to_cart", "purchase", "search"]
_mock_client = None  # one in-memory server per process


def get_collection(settings: Settings):
    global _mock_client
    if settings.mongo_uri:
        from pymongo import MongoClient

        client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
    else:
        import mongomock

        if _mock_client is None:
            _mock_client = mongomock.MongoClient()
        client = _mock_client
    return client[settings.mongo_db]["events"]


def seed_events(settings: Settings, n: int = 2000, seed: int = 11) -> int:
    """Generate click-stream events (idempotent: wipes and rewrites the collection)."""
    rng = random.Random(seed)
    coll = get_collection(settings)
    coll.delete_many({})
    base = datetime(2026, 1, 1)
    docs = []
    for i in range(n):
        etype = rng.choices(EVENT_TYPES, weights=[60, 20, 8, 12])[0]
        doc = {
            "event_id": f"evt-{i:06d}",
            "type": etype,
            "customer_id": rng.randint(1, 200),
            "ts": base + timedelta(minutes=rng.randint(0, 60 * 24 * 30)),
            "device": rng.choice(["mobile", "desktop", "tablet"]),
        }
        if etype in ("page_view", "add_to_cart", "purchase"):
            doc["product_sku"] = rng.choice(SKUS)
        if etype == "search":
            doc["query"] = rng.choice(["python", "mystery", "travel", "science", "history"])
        docs.append(doc)
    coll.insert_many(docs)
    coll.create_index("type")  # indexes speed up filters/aggregations
    coll.create_index([("customer_id", 1), ("ts", 1)])
    log.info("Mongo events seeded: %d", n)
    return n


def extract_events(settings: Settings) -> pd.DataFrame:
    coll = get_collection(settings)
    docs = list(coll.find({}, {"_id": 0}))
    df = pd.DataFrame(docs)
    log.info("Extracted %d events from MongoDB", len(df))
    return df


def funnel(settings: Settings) -> pd.DataFrame:
    """Aggregation-pipeline example: event counts per type and device."""
    coll = get_collection(settings)
    pipeline = [
        {"$group": {"_id": {"type": "$type", "device": "$device"}, "events": {"$sum": 1}}},
        {"$sort": {"events": -1}},
        {"$project": {"_id": 0, "type": "$_id.type", "device": "$_id.device", "events": 1}},
    ]
    return pd.DataFrame(list(coll.aggregate(pipeline)))
