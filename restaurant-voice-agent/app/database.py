"""MongoDB connection and indexes.

Collections
-----------
restaurants   {_id, name, currency, delivery_fee_minor, accepts_pickup, accepts_delivery, owner_password_hash}
menu_items    {_id, restaurant_id, name, description, category, price_minor, available, options[]}
orders        {_id, reference, restaurant_id, voice_session_id, status, lines[], subtotal_minor,
               delivery_fee_minor, total_minor, currency, fulfillment, customer{name,phone},
               delivery_address, quote_hash, expires_at, confirmed_at, created_at, updated_at}
voice_sessions{_id(client_call_id), restaurant_id, openai_session_id, call_secret_hash, status,
               created_at, ended_at, end_reason, usage}   # metadata only: no audio, no transcripts
"""
from pymongo import ASCENDING, MongoClient
from pymongo.database import Database

from .config import Settings


def connect(settings: Settings) -> Database:
    client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000)
    return client[settings.mongodb_database]


def init_indexes(db: Database) -> None:
    db.menu_items.create_index([("restaurant_id", ASCENDING), ("category", ASCENDING)])
    db.orders.create_index("reference", unique=True)
    # one order per call: this is also the duplicate-submission guard
    db.orders.create_index("voice_session_id", unique=True)
    db.orders.create_index([("restaurant_id", ASCENDING), ("status", ASCENDING)])
    # Unconfirmed orders carry expires_at and are purged by MongoDB; confirming removes the field.
    db.orders.create_index("expires_at", expireAfterSeconds=0)
    db.voice_sessions.create_index("openai_session_id")
