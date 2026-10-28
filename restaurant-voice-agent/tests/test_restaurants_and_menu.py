"""Restaurant CRUD (admin + owner scopes, archive policy) and menu delete. No network, no OpenAI spend."""
import json

import pytest

from app.security import order_access_token
from app.services.live_session import CallController
from tests.conftest import FakeConnect, FakeLive, SDP, call_id, make_settings, wait_for

H = {"Origin": "http://testserver"}
NEW = {"id": "resto-c", "name": "Resto C", "currency": "PKR", "delivery_fee_minor": 10000, "owner_password": "pw-resto-c-123"}


def settings_with_admin(**kw):
    return make_settings(admin_password="admin-pass-1", **kw)


def owner_login(c, rid="resto-a"):
    assert c.post("/api/owner/login", json={"restaurant_id": rid, "password": f"pw-{rid}"}, headers=H).status_code == 200


def admin_login(c):
    assert c.post("/api/admin/login", json={"password": "admin-pass-1"}, headers=H).status_code == 200


def delete(c, rid, **body):
    return c.request("DELETE", f"/api/restaurants/{rid}", json=body, headers=H)


# ===== restaurant CRUD =====
def test_unauthenticated_and_disabled_admin_are_rejected(make_client):
    with make_client() as c:  # no ADMIN_PASSWORD configured
        assert c.get("/api/restaurants").status_code == 401
        assert c.post("/api/restaurants", json=NEW, headers=H).status_code == 401
        assert c.post("/api/admin/login", json={"password": "anything"}, headers=H).status_code == 403
        assert c.patch("/api/restaurants/resto-a", json={"name": "x"}, headers=H).status_code == 401
        assert delete(c, "resto-a", confirm_id="resto-a", password="x").status_code == 401


def test_admin_create_list_get_update(make_client, db):
    with make_client(settings=settings_with_admin()) as c:
        assert c.post("/api/admin/login", json={"password": "wrong"}, headers=H).status_code == 401
        admin_login(c)
        r = c.post("/api/restaurants", json=NEW, headers=H)
        assert r.status_code == 201 and r.json()["id"] == "resto-c" and r.json()["status"] == "active"
        assert "password" not in r.text and "hash" not in r.text
        stored = db.restaurants.find_one({"_id": "resto-c"})
        assert stored["owner_password_hash"].startswith("scrypt$") and "pw-resto-c-123" not in json.dumps(stored, default=str)
        assert {x["id"] for x in c.get("/api/restaurants").json()["restaurants"]} == {"resto-a", "resto-b", "resto-c"}
        assert c.get("/api/restaurants/resto-b").json()["name"] == "Resto B"
        up = c.patch("/api/restaurants/resto-c", json={"name": " Renamed ", "delivery_fee_minor": 500, "accepts_pickup": False}, headers=H)
        assert up.status_code == 200 and up.json()["name"] == "Renamed" and up.json()["accepts_pickup"] is False
        assert c.get("/api/restaurants/nope").status_code == 404
    with make_client(settings=settings_with_admin()) as c2:  # the new owner can log in with the password that was set
        assert c2.post("/api/owner/login", json={"restaurant_id": "resto-c", "password": "pw-resto-c-123"}, headers=H).status_code == 200


@pytest.mark.parametrize("bad", [{"id": "Bad ID"}, {"id": "a"}, {"id": "-x-"}, {"id": "ok-id", "currency": "pkr"},
                                 {"id": "ok-id", "owner_password": "short"}, {"id": "ok-id", "delivery_fee_minor": -1},
                                 {"id": "ok-id", "name": "   "}, {"id": "ok-id", "owner_password_hash": "x"}, {"id": "ok-id", "status": "archived"}])
def test_create_validation_and_protected_fields(make_client, bad):
    with make_client(settings=settings_with_admin()) as c:
        admin_login(c)
        assert c.post("/api/restaurants", json={**NEW, **bad}, headers=H).status_code == 422


def test_duplicate_restaurant_id_rejected_and_nothing_overwritten(make_client, db):
    before = db.restaurants.find_one({"_id": "resto-a"})
    with make_client(settings=settings_with_admin()) as c:
        admin_login(c)
        assert c.post("/api/restaurants", json={**NEW, "id": "resto-a", "name": "Hijack"}, headers=H).status_code == 409
    assert db.restaurants.find_one({"_id": "resto-a"}) == before


def test_owner_scope_isolation_and_protected_fields(make_client, db):
    with make_client(settings=settings_with_admin()) as c:
        owner_login(c, "resto-a")
        assert [x["id"] for x in c.get("/api/restaurants").json()["restaurants"]] == ["resto-a"]
        assert c.get("/api/restaurants/resto-a").status_code == 200
        assert c.get("/api/restaurants/resto-b").status_code == 404                       # foreign restaurant
        assert c.patch("/api/restaurants/resto-b", json={"name": "pwned"}, headers=H).status_code == 404
        assert c.post("/api/restaurants", json=NEW, headers=H).status_code == 403          # owners cannot create
        assert delete(c, "resto-b", confirm_id="resto-b", password="pw-resto-b").status_code == 404
        for protected in ({"_id": "x"}, {"owner_password_hash": "x"}, {"status": "archived"}, {"currency": "USD"}, {"id": "z"}):
            assert c.patch("/api/restaurants/resto-a", json=protected, headers=H).status_code == 422
        assert c.patch("/api/restaurants/resto-a", json={}, headers=H).status_code == 422
        assert c.patch("/api/restaurants/resto-a", json={"accepts_pickup": False, "accepts_delivery": False}, headers=H).status_code == 422
        assert c.patch("/api/restaurants/resto-a", json={"delivery_fee_minor": 1}, headers=H).json()["delivery_fee_minor"] == 1
    a, b = db.restaurants.find_one({"_id": "resto-a"}), db.restaurants.find_one({"_id": "resto-b"})
    assert a["currency"] == "PKR" and a["owner_password_hash"].startswith("scrypt$") and "status" not in a
    assert b["name"] == "Resto B" and b["delivery_fee_minor"] == 0


def test_cross_origin_restaurant_writes_rejected(make_client):
    with make_client(settings=settings_with_admin()) as c:
        evil = {"Origin": "https://evil.example"}
        assert c.post("/api/admin/login", json={"password": "admin-pass-1"}, headers=evil).status_code == 403
        owner_login(c)
        assert c.patch("/api/restaurants/resto-a", json={"name": "x"}, headers=evil).status_code == 403


# ===== archive (the deletion policy) =====
def make_confirmed_order(db):
    ctl = CallController(db, "resto-a", "d" * 32)
    assert ctl.execute_tool("set_order_items", json.dumps({"items": [{"item_id": "a-naan", "quantity": 1}]}))["ok"]
    assert ctl.execute_tool("set_customer_details", json.dumps({"customer_name": "Sara", "phone": "03001234567", "fulfillment": "pickup"}))["ok"]
    q = ctl.execute_tool("request_confirmation", "{}")
    ctl.customer_replied = True
    assert ctl.execute_tool("confirm_order", json.dumps({"quote_hash": q["quote_hash"], "customer_confirmed": True}))["confirmed"]
    return db.orders.find_one({"voice_session_id": "d" * 32})


def test_archive_requires_confirmation_and_password(make_client, db):
    with make_client(settings=settings_with_admin()) as c:
        owner_login(c)
        assert delete(c, "resto-a", confirm_id="resto-a", password="wrong").status_code == 401
        assert delete(c, "resto-a", confirm_id="resto-b", password="pw-resto-a").status_code == 422
        assert delete(c, "resto-a", password="pw-resto-a").status_code == 422
        assert c.delete("/api/restaurants/resto-a", headers=H).status_code == 422             # no body at all
        assert "status" not in db.restaurants.find_one({"_id": "resto-a"})


def test_archive_preserves_history_and_blocks_everything_new(make_client, db):
    order = make_confirmed_order(db)
    db.voice_sessions.insert_one({"_id": "e" * 32, "restaurant_id": "resto-a", "status": "ended", "call_secret_hash": "x"})
    live = FakeLive()
    with make_client(settings=settings_with_admin(), live_client=live) as c:
        owner_login(c)
        r = delete(c, "resto-a", confirm_id="resto-a", password="pw-resto-a")
        assert r.status_code == 200 and r.json()["status"] == "archived"
        assert c.get("/api/owner/me").status_code == 401                                      # owner session ended
        # --- nothing new is possible ---
        assert c.get("/api/menu").status_code == 404 and c.get("/api/menu?restaurant_id=resto-a").status_code == 404
        assert c.get("/call?restaurant_id=resto-a").status_code == 404
        assert c.post("/api/call/session", json={"sdp": SDP, "client_call_id": call_id(), "restaurant_id": "resto-a"}, headers=H).status_code == 404
        assert live.calls == []                                                               # OpenAI never contacted
        assert c.post("/api/owner/login", json={"restaurant_id": "resto-a", "password": "pw-resto-a"}, headers=H).status_code == 401
        ctl = CallController(db, "resto-a", "f" * 32)
        res = ctl.execute_tool("set_order_items", json.dumps({"items": [{"item_id": "a-naan", "quantity": 1}]}))
        assert res["ok"] is False and res["error_code"] == "unknown_restaurant"
        assert db.orders.count_documents({"voice_session_id": "f" * 32}) == 0
        # --- history untouched and still readable ---
        token = order_access_token("test-secret", order["_id"])
        page = c.get(f"/confirmation/{order['reference']}?t={token}")
        assert page.status_code == 200 and "Resto A" in page.text
        assert delete(c, "resto-a", confirm_id="resto-a", password="pw-resto-a").status_code == 401   # already archived
    assert db.orders.count_documents({"restaurant_id": "resto-a"}) == 1
    assert db.voice_sessions.count_documents({"restaurant_id": "resto-a"}) == 1
    assert db.menu_items.count_documents({"restaurant_id": "resto-a"}) == 3
    assert db.restaurants.find_one({"_id": "resto-a"})["status"] == "archived"
    assert db.restaurants.find_one({"_id": "resto-b"}).get("status") is None                    # B untouched


def test_admin_archive_hides_from_default_list_but_id_stays_reserved(make_client, db):
    with make_client(settings=settings_with_admin()) as c:
        admin_login(c)
        assert delete(c, "resto-b", confirm_id="resto-b", password="nope").status_code == 401
        assert delete(c, "resto-b", confirm_id="resto-b", password="admin-pass-1").status_code == 200
        assert {x["id"] for x in c.get("/api/restaurants").json()["restaurants"]} == {"resto-a"}
        assert {x["id"] for x in c.get("/api/restaurants?include_archived=true").json()["restaurants"]} == {"resto-a", "resto-b"}
        assert c.post("/api/restaurants", json={**NEW, "id": "resto-b"}, headers=H).status_code == 409


def test_archive_closes_active_calls_immediately(make_client, db):
    conn = FakeConnect()
    with make_client(settings=settings_with_admin(), connect=conn) as c:
        r = c.post("/api/call/session", json={"sdp": SDP, "client_call_id": call_id()}, headers=H)
        assert r.status_code == 200
        owner_login(c)
        out = delete(c, "resto-a", confirm_id="resto-a", password="pw-resto-a")
        assert out.json()["active_calls_closed"] == 1
        assert wait_for(lambda: "session.close" in conn.ws.types(), 2)
        conn.ws.incoming.put({"type": "session.closed", "usage": {"seconds": 1}})
        assert wait_for(lambda: db.voice_sessions.find_one({"_id": call_id()})["status"] == "ended")
        assert db.voice_sessions.find_one({"_id": call_id()})["end_reason"] == "restaurant_unavailable"
        assert c.app.state.live.active_count() == 0


# ===== menu delete =====
def test_menu_delete_requires_login_and_is_scoped(make_client, db):
    with make_client() as c:
        assert c.delete("/api/menu/items/a-naan", headers=H).status_code == 401
        owner_login(c)
        assert c.delete("/api/menu/items/b-pizza", headers=H).status_code == 404            # other restaurant's id
        assert db.menu_items.find_one({"_id": "b-pizza"}) is not None
        assert c.delete("/api/menu/items/does-not-exist", headers=H).status_code == 404
        assert c.delete("/api/menu/items/a-naan", headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.delete("/api/menu/items/a-naan", headers=H).json() == {"ok": True, "id": "a-naan"}
        assert c.delete("/api/menu/items/a-naan", headers=H).status_code == 404             # second delete
        assert {i["id"] for i in c.get("/api/menu").json()["items"]} == {"a-karahi", "a-daal"}
        assert {i["id"] for i in c.get("/api/menu?restaurant_id=resto-b").json()["items"]} == {"b-pizza"}


def test_deleted_items_cannot_be_ordered_or_confirmed(db):
    ctl = CallController(db, "resto-a", "c" * 32)
    items = json.dumps({"items": [{"item_id": "a-naan", "quantity": 1}]})
    assert ctl.execute_tool("set_order_items", items)["ok"]
    assert ctl.execute_tool("set_customer_details", json.dumps({"customer_name": "S", "phone": "03001234567", "fulfillment": "pickup"}))["ok"]
    q = ctl.execute_tool("request_confirmation", "{}")
    ctl.customer_replied = True
    db.menu_items.delete_one({"_id": "a-naan", "restaurant_id": "resto-a"})                 # owner deletes before confirm
    res = ctl.execute_tool("confirm_order", json.dumps({"quote_hash": q["quote_hash"], "customer_confirmed": True}))
    assert res["ok"] is False and res["error_code"] == "unknown_item"
    assert db.orders.find_one({"voice_session_id": "c" * 32})["status"] != "confirmed"
    other = CallController(db, "resto-a", "b" * 32)
    assert other.execute_tool("set_order_items", items)["error_code"] == "unknown_item"
    unavailable = json.dumps({"items": [{"item_id": "a-daal", "quantity": 1}]})
    assert other.execute_tool("set_order_items", unavailable)["error_code"] == "item_unavailable"


# ===== admin secret handling =====
def test_admin_password_never_appears_in_responses_or_logs(make_client, caplog):
    secret = "admin-pass-1"
    with make_client(settings=settings_with_admin()) as c:
        seen = [c.post("/api/admin/login", json={"password": "wrong-guess"}, headers=H),
                c.post("/api/admin/login", json={"password": secret}, headers=H),
                c.get("/api/restaurants"), c.get("/api/restaurants/resto-a"),
                c.post("/api/restaurants", json={**NEW, "id": "resto-a"}, headers=H),
                delete(c, "resto-b", confirm_id="resto-b", password="nope"),
                c.get("/api/menu"), c.get("/call"), c.get("/menu")]
        assert all(secret not in r.text and secret not in str(r.headers) for r in seen)
        assert "admin_password" not in "".join(r.text for r in seen)
    assert secret not in caplog.text


def test_admin_session_dies_if_admin_access_is_disabled_and_archive_twice_is_safe(make_client, db):
    with make_client(settings=settings_with_admin()) as c:
        admin_login(c)
        assert delete(c, "resto-b", confirm_id="resto-b", password="admin-pass-1").status_code == 200
        assert delete(c, "resto-b", confirm_id="resto-b", password="admin-pass-1").status_code == 404   # repeat: no-op
        assert db.restaurants.find_one({"_id": "resto-b"})["status"] == "archived"
        c.app.state.settings.admin_password = ""                       # ADMIN_PASSWORD removed: old cookie is worthless
        assert c.get("/api/restaurants").status_code == 401
        assert c.post("/api/restaurants", json=NEW, headers=H).status_code == 401


def test_owner_cookie_of_an_archived_restaurant_cannot_write_menu(make_client, db):
    with make_client(settings=settings_with_admin()) as owner, make_client(settings=settings_with_admin()) as admin:
        owner_login(owner)
        admin_login(admin)
        assert delete(admin, "resto-a", confirm_id="resto-a", password="admin-pass-1").status_code == 200
        body = {"name": "Tea", "category": "Drinks", "price_minor": 100}
        assert owner.post("/api/menu/items", json=body, headers=H).status_code == 401
        assert owner.delete("/api/menu/items/a-naan", headers=H).status_code == 401
        assert owner.patch("/api/restaurants/resto-a", json={"name": "x"}, headers=H).status_code == 404
        assert db.menu_items.count_documents({"restaurant_id": "resto-a"}) == 3
