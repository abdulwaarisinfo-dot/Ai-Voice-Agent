import asyncio
import json

import pytest

from app.models.schemas import CallSessionIn
from app.services.live_session import CallError, LiveService
from tests.conftest import KEY, SDP, FakeConnect, FakeLive, call_id, make_settings, tool_event, wait_for

H = {"Origin": "http://testserver"}


def post_session(c, n=1, **extra):
    return c.post("/api/call/session", json={"sdp": SDP, "client_call_id": call_id(n), **extra}, headers=H)


# ===== pages =====
def test_pages_render_without_creating_a_voice_session(make_client):
    live = FakeLive()
    with make_client(live_client=live) as c:
        for path in ("/call", "/menu", "/call?restaurant_id=resto-b"):
            assert c.get(path).status_code == 200
        assert "Call restaurant" in c.get("/call").text and "Cancel" in c.get("/call").text
        assert c.get("/", follow_redirects=False).status_code in (302, 307)
        assert c.get("/call?restaurant_id=nope").status_code == 404
    assert live.calls == []  # opening or refreshing pages never reaches OpenAI


def test_security_headers_and_no_key_in_pages(make_client):
    with make_client() as c:
        r = c.get("/call")
        assert "microphone=(self)" in r.headers["permissions-policy"] and "script-src 'self'" in r.headers["content-security-policy"]
        for path in ("/call", "/menu", "/static/js/call.js", "/static/js/menu.js"):
            assert KEY not in c.get(path).text


# ===== menu =====
def test_public_menu_is_scoped(make_client):
    with make_client() as c:
        a = c.get("/api/menu").json()
        b = c.get("/api/menu?restaurant_id=resto-b").json()
        assert a["restaurant"]["id"] == "resto-a" and {i["name"] for i in b["items"]} == {"Pizza"}
        assert c.get("/api/menu?restaurant_id=nope").status_code == 404


def login(c, rid="resto-a", pw=None):
    return c.post("/api/owner/login", json={"restaurant_id": rid, "password": pw or f"pw-{rid}"}, headers=H)


def test_menu_management_requires_owner_login(make_client):
    body = {"name": "Tea", "category": "Drinks", "price_minor": 5000}
    with make_client() as c:
        assert c.post("/api/menu/items", json=body, headers=H).status_code == 401
        assert c.patch("/api/menu/items/a-naan/availability", json={"available": False}, headers=H).status_code == 401
        assert login(c, pw="wrong").status_code == 401
        assert login(c, rid="nope").status_code == 401
        assert login(c).status_code == 200


def test_owner_can_add_edit_and_toggle_and_is_isolated(make_client, db):
    with make_client() as c:
        login(c)
        added = c.post("/api/menu/items", json={"name": "Tea", "category": "Drinks", "price_minor": 5000, "options": ["no sugar"]}, headers=H)
        assert added.status_code == 201 and added.json()["price"] == "PKR 50.00"
        iid = added.json()["id"]
        assert db.menu_items.find_one({"_id": iid})["restaurant_id"] == "resto-a"
        edited = c.put(f"/api/menu/items/{iid}", json={"name": "Chai", "category": "Drinks", "price_minor": 6000}, headers=H)
        assert edited.json()["name"] == "Chai"
        assert c.patch(f"/api/menu/items/{iid}/availability", json={"available": False}, headers=H).json()["available"] is False
        # owner of A cannot touch B's items
        assert c.patch("/api/menu/items/b-pizza/availability", json={"available": False}, headers=H).status_code == 404
        assert c.put("/api/menu/items/b-pizza", json={"name": "x", "category": "y", "price_minor": 1}, headers=H).status_code == 404
        assert db.menu_items.find_one({"_id": "b-pizza"})["available"] is True


@pytest.mark.parametrize("bad", [{"name": "", "category": "x", "price_minor": 1}, {"name": "x", "category": "y", "price_minor": -5},
                                 {"name": "x", "category": "y", "price_minor": 10**9}, {"name": "x", "category": "y", "price_minor": 1.5}])
def test_menu_validation(make_client, bad):
    with make_client() as c:
        login(c)
        assert c.post("/api/menu/items", json=bad, headers=H).status_code == 422


def test_cross_origin_writes_rejected(make_client):
    with make_client() as c:
        login(c)
        r = c.post("/api/menu/items", json={"name": "x", "category": "y", "price_minor": 1}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        assert c.post("/api/call/session", json={"sdp": SDP, "client_call_id": call_id()}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_login_is_rate_limited(make_client):
    with make_client() as c:
        codes = [login(c, pw="bad").status_code for _ in range(12)]
        assert codes[:10] == [401] * 10 and 429 in codes[10:]


# ===== session creation =====
def test_session_creation_uses_official_contract_and_leaks_nothing(make_client):
    live, conn = FakeLive(), FakeConnect()
    with make_client(live_client=live, connect=conn) as c:
        r = post_session(c)
        assert r.status_code == 200
        body = r.json()
        assert body["transport"] == {"type": "webrtc", "sdp": "v=0 ANSWER"} and body["call_secret"]
        raw = r.text
        assert KEY not in raw and "live_test_1" not in raw  # no key, no OpenAI session id
        assert len(live.calls) == 1
        sent = live.calls[0]
        assert sent["transport"] == {"type": "webrtc", "sdp": SDP}
        s = sent["session"]
        assert s["model"] == "gpt-live-1" and s["delegation"]["type"] == "responses"
        assert {t["name"] for t in s["delegation"]["responses"]["tools"]} == {
            "get_menu", "set_order_items", "set_customer_details", "get_order_summary", "request_confirmation", "confirm_order"}
        assert "Resto A" in s["instructions"] and "audio" not in s  # WebRTC: no audio.format
        assert conn.url == "wss://api.openai.com/v1/live/sessions/live_test_1/attach"
        assert conn.headers["Authorization"] == f"Bearer {KEY}"


def test_session_request_validation(make_client):
    with make_client() as c:
        assert c.post("/api/call/session", json={"sdp": "nope", "client_call_id": call_id()}, headers=H).status_code == 422
        assert c.post("/api/call/session", json={"sdp": SDP, "client_call_id": "short"}, headers=H).status_code == 422
        assert c.post("/api/call/session", json={"sdp": "v=0" + "x" * 70000, "client_call_id": call_id()}, headers=H).status_code == 422
        assert post_session(c, restaurant_id="unknown").status_code == 404


def test_openai_failure_is_safe_cleaned_up_and_retryable(make_client, db, caplog):
    live = FakeLive(fail=True)
    with make_client(live_client=live) as c:
        r = post_session(c, 1)
        assert r.status_code == 502 and "call_secret" not in r.text and "exploded" not in r.text and KEY not in r.text
        assert db.voice_sessions.find_one({"_id": call_id(1)})["status"] == "failed"
        assert c.app.state.live.active_count() == 0  # nothing left behind
        live.fail = False
        assert post_session(c, 2).status_code == 200  # retry works
    assert KEY not in caplog.text and "exploded" not in caplog.text


def test_missing_api_key_gives_clear_503(make_client):
    with make_client(settings=make_settings(openai_api_key="")) as c:
        assert post_session(c).status_code == 503


def test_sideband_failure_returns_503_and_no_answer(make_client, db):
    with make_client(connect=FakeConnect(fail=True)) as c:
        r = post_session(c)
        assert r.status_code == 503 and "ANSWER" not in r.text
        assert wait_for(lambda: db.voice_sessions.find_one({"_id": call_id()})["status"] == "failed")


def test_duplicate_and_repeated_requests_do_not_create_duplicate_sessions(make_client):
    live = FakeLive()
    with make_client(live_client=live) as c:
        assert post_session(c, 1).status_code == 200
        assert post_session(c, 1).status_code == 409
        assert len(live.calls) == 1


def test_call_rate_limit_and_concurrency_cap(make_client):
    with make_client() as c:
        codes = [post_session(c, n).status_code for n in range(1, 8)]
        assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    with make_client(settings=make_settings(max_concurrent_calls=1)) as c:
        assert post_session(c, 11).status_code == 200
        assert post_session(c, 12).status_code == 503


# ===== lifecycle: ready / end / cancel / watchdog =====
def test_end_call_closes_session_and_never_confirms_an_order(make_client, db):
    conn = FakeConnect()
    with make_client(connect=conn) as c:
        secret = post_session(c).json()["call_secret"]
        hdr = {**H, "X-Call-Secret": secret}
        assert c.post("/api/call/ready", json={"client_call_id": call_id()}, headers=hdr).status_code == 200
        assert wait_for(lambda: "session.commentary.append" in conn.ws.types())  # greeting only after browser says ready
        conn.ws.incoming.put(tool_event("t1", "set_order_items", {"items": [{"item_id": "a-naan", "quantity": 1}]}))
        assert wait_for(lambda: "response.create" in conn.ws.types())
        assert c.post("/api/call/end", json={"client_call_id": call_id()}, headers=hdr).status_code == 200
        assert wait_for(lambda: "session.close" in conn.ws.types())
        conn.ws.incoming.put({"type": "session.closed", "usage": {"seconds": 5}})
        assert wait_for(lambda: db.voice_sessions.find_one({"_id": call_id()})["status"] == "ended")
        assert db.voice_sessions.find_one({"_id": call_id()})["usage"] == {"seconds": 5}
        orders = list(db.orders.find({}))
        assert len(orders) == 1 and orders[0]["status"] == "pending"  # not confirmed, not discarded
        assert c.get(f"/api/call/status?client_call_id={call_id()}", headers={"X-Call-Secret": secret}).json()["state"] == "ended"


def test_call_start_and_end_alone_create_no_order(make_client, db):
    conn = FakeConnect()
    with make_client(connect=conn) as c:
        secret = post_session(c).json()["call_secret"]
        c.post("/api/call/end", json={"client_call_id": call_id()}, headers={**H, "X-Call-Secret": secret})
        assert wait_for(lambda: "session.close" in conn.ws.types())
        conn.ws.incoming.put({"type": "session.closed"})
        assert db.orders.count_documents({}) == 0


def test_call_endpoints_require_the_call_secret(make_client):
    with make_client() as c:
        secret = post_session(c).json()["call_secret"]
        for hdrs in ({}, {"X-Call-Secret": "wrong"}):
            assert c.get(f"/api/call/status?client_call_id={call_id()}", headers=hdrs).status_code == 404
            assert c.post("/api/call/end", json={"client_call_id": call_id()}, headers={**H, **hdrs}).status_code == 404
            assert c.post("/api/call/ready", json={"client_call_id": call_id()}, headers={**H, **hdrs}).status_code == 404
        assert c.get(f"/api/call/status?client_call_id={call_id(9)}", headers={"X-Call-Secret": secret}).status_code == 404


def test_cancel_after_session_exists_closes_it(make_client, db):
    conn = FakeConnect()
    with make_client(connect=conn) as c:
        post_session(c)
        r = c.post("/api/call/cancel", json={"client_call_id": call_id()}, headers=H)
        assert r.json() == {"ok": True, "known": True}
        assert wait_for(lambda: "session.close" in conn.ws.types())
        conn.ws.incoming.put({"type": "session.closed"})
        assert wait_for(lambda: db.voice_sessions.find_one({"_id": call_id()})["status"] == "cancelled")
        assert c.post("/api/call/cancel", json={"client_call_id": call_id(5)}, headers=H).json()["known"] is False


async def test_cancel_while_openai_is_still_creating_the_session(db):
    """Late response must not start a call; the server-side session is closed via the sideband."""
    gate, conn = asyncio.Event(), FakeConnect()
    svc = LiveService(db, make_settings(), FakeLive(gate=gate), connect=conn)
    task = asyncio.create_task(svc.create_call(db.restaurants.find_one({"_id": "resto-a"}), call_id(), SDP))
    await asyncio.sleep(0.05)
    assert svc.request_cancel(call_id()) is True
    gate.set()
    with pytest.raises(CallError) as e:
        await task
    assert e.value.status == 409
    for _ in range(100):
        if "session.close" in conn.ws.types():
            break
        await asyncio.sleep(0.02)
    assert "session.close" in conn.ws.types()
    conn.ws.incoming.put({"type": "session.closed"})
    await asyncio.sleep(0.1)
    assert db.voice_sessions.find_one({"_id": call_id()})["status"] == "cancelled"


def test_browser_that_disappears_gets_its_call_closed(make_client):
    conn = FakeConnect()
    with make_client(settings=make_settings(idle_timeout_seconds=1), connect=conn) as c:
        secret = post_session(c).json()["call_secret"]
        c.post("/api/call/ready", json={"client_call_id": call_id()}, headers={**H, "X-Call-Secret": secret})
        assert wait_for(lambda: "session.close" in conn.ws.types(), timeout=4)  # no heartbeat -> server closes (cost guard)


def test_max_call_duration_is_enforced(make_client):
    conn = FakeConnect()
    with make_client(settings=make_settings(max_call_seconds=1), connect=conn) as c:
        post_session(c)
        assert wait_for(lambda: "session.close" in conn.ws.types(), timeout=4)


# ===== end to end: tools -> confirmed order -> confirmation page =====
def test_end_to_end_order_then_confirmation_page(make_client, db):
    conn = FakeConnect()
    with make_client(connect=conn) as c:
        secret = post_session(c).json()["call_secret"]
        hdr = {"X-Call-Secret": secret}
        q = f"/api/call/status?client_call_id={call_id()}"
        assert c.get(q, headers=hdr).json()["order"] is None

        def feed(cid, name, args):
            before = conn.ws.types().count("response.create")
            conn.ws.incoming.put(tool_event(cid, name, args))
            assert wait_for(lambda: conn.ws.types().count("response.create") == before + 1)
            out = [m for m in conn.ws.sent if m["type"] == "response.item.create" and m["item"]["call_id"] == cid][-1]
            return json.loads(out["item"]["output"])

        assert feed("1", "set_order_items", {"items": [{"item_id": "a-karahi", "quantity": 2}]})["ok"]
        assert feed("2", "set_customer_details", {"customer_name": "Sara", "phone": "03001234567", "fulfillment": "delivery", "delivery_address": "12 Canal Rd"})["ok"]
        rb = feed("3", "request_confirmation", {})
        assert c.get(q, headers=hdr).json()["order"] is None  # read back is not an order
        conn.ws.incoming.put({"type": "session.input_transcript.delta", "delta": "yes"})
        import time; time.sleep(0.15)
        done = feed("4", "confirm_order", {"quote_hash": rb["quote_hash"], "customer_confirmed": True})
        assert done["confirmed"] is True

        status = c.get(q, headers=hdr).json()
        url = status["order"]["url"]
        assert status["order"]["reference"] == done["reference"]
        page = c.get(url)
        assert page.status_code == 200 and done["reference"] in page.text and "PKR 2,748.00" in page.text  # 2*1299 + 150 fee
        assert "12 Canal Rd" in page.text and "Resto A" in page.text and "Back to menu" in page.text
        api = c.get("/api/orders/" + url.split("/confirmation/")[1].replace("?t=", "?t=")).json()
        assert api["total"] == "PKR 2,748.00" and api["delivery_fee"] == "PKR 150.00"
        # tampered / missing token / unknown ref -> identical 404
        ref = done["reference"]
        for bad in (f"/confirmation/{ref}", f"/confirmation/{ref}?t=deadbeef", "/confirmation/ORD-AAAAAAAA?t=x", f"/api/orders/{ref}?t=nope", f"/api/orders/{ref}"):
            assert c.get(bad).status_code == 404
        assert "Order not found" in c.get(f"/confirmation/{ref}?t=deadbeef").text


def test_visiting_a_confirmation_url_without_confirmed_order_proves_nothing(make_client, db):
    with make_client() as c:
        assert c.get("/confirmation/ORD-ABCDEFGH?t=abc").status_code == 404
        assert db.orders.count_documents({}) == 0
