"""Shared fixtures. No network, no real MongoDB, no OpenAI spend."""
import asyncio
import json
import queue
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import init_indexes
from app.main import create_app
from app.security import hash_password

KEY = "sk-test-secret-key"
SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"


def make_settings(**kw) -> Settings:
    base = dict(openai_api_key=KEY, session_secret="test-secret", app_env="test", default_restaurant_id="resto-a",
                max_call_seconds=600, idle_timeout_seconds=30, max_concurrent_calls=5)
    base.update(kw)
    return Settings(_env_file=None, **base)


def seed(db):
    now = datetime.now(timezone.utc)
    for rid, name, fee in (("resto-a", "Resto A", 15000), ("resto-b", "Resto B", 0)):
        db.restaurants.insert_one({"_id": rid, "name": name, "currency": "PKR", "delivery_fee_minor": fee,
                                   "accepts_pickup": True, "accepts_delivery": True, "owner_password_hash": hash_password(f"pw-{rid}")})
    rows = [("a-karahi", "resto-a", "Chicken Karahi", "Mains", 129900, True, ["extra spicy", "mild"]),
            ("a-naan", "resto-a", "Garlic Naan", "Breads", 12000, True, []),
            ("a-daal", "resto-a", "Daal Mash", "Mains", 59900, False, []),
            ("b-pizza", "resto-b", "Pizza", "Mains", 99900, True, [])]
    for iid, rid, name, cat, price, avail, opts in rows:
        db.menu_items.insert_one({"_id": iid, "restaurant_id": rid, "name": name, "description": "", "category": cat,
                                  "price_minor": price, "available": avail, "options": opts, "created_at": now, "updated_at": now})


@pytest.fixture
def db():
    d = mongomock.MongoClient().db
    init_indexes(d)
    seed(d)
    return d


# ---------- fakes for OpenAI ----------
class FakeLive:
    def __init__(self, fail=False, gate: asyncio.Event | None = None):
        self.calls, self.fail, self.gate = [], fail, gate
        self.live = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            err = RuntimeError("upstream exploded")
            err.status_code = 500
            raise err
        return SimpleNamespace(session=SimpleNamespace(id="live_test_1"), transport=SimpleNamespace(sdp="v=0 ANSWER"))


class FakeWS:
    """Thread-safe so the test thread can feed events to the app's event loop."""
    def __init__(self):
        self.incoming, self.sent, self.closed = queue.Queue(), [], False

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        self.closed = True
        self.incoming.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            try:
                item = self.incoming.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
            if item is None:
                raise StopAsyncIteration
            return json.dumps(item)

    def types(self):
        return [m["type"] for m in self.sent]


class FakeConnect:
    def __init__(self, fail=False):
        self.ws, self.fail, self.url, self.headers = FakeWS(), fail, None, None

    def __call__(self, url, headers):
        self.url, self.headers = url, headers
        return self

    async def __aenter__(self):
        if self.fail:
            raise OSError("cannot attach")
        return self.ws

    async def __aexit__(self, *a):
        return False


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def live():
    return FakeLive()


@pytest.fixture
def connect():
    return FakeConnect()


@pytest.fixture
def make_client(db):
    """Factory: with make_client(...) as c:  -> TestClient sharing one event loop for the whole test."""
    def _make(settings=None, live_client=None, connect=None):
        app = create_app(settings or make_settings(), db=db, live_client=live_client or FakeLive(), sideband_connect=connect or FakeConnect())
        return TestClient(app, base_url="http://testserver")
    return _make


def call_id(n=1) -> str:
    return f"{n:032x}"


def tool_event(cid: str, name: str, args: dict) -> dict:
    return {"type": "response.event", "delegation_id": "d1", "event": {
        "type": "response.output_item.done", "item": {"type": "function_call", "call_id": cid, "name": name, "arguments": json.dumps(args)}}}
