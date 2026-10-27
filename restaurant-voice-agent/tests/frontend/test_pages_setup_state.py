"""/call and /menu when the *implicit default* restaurant has no database record.

Contract under test:
  * missing implicit default      -> HTML setup page (200, no-store), call button disabled, no scripts, no data
  * explicitly supplied bad id   -> still 404
  * database failures            -> still 500 (never disguised as "not configured")
  * API/voice paths              -> unchanged: 404, and nothing reaches OpenAI or the database
  * configured restaurants       -> unchanged
No live MongoDB and no OpenAI calls: mongomock + the fakes from conftest.
"""
import re
from types import SimpleNamespace

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect, OperationFailure, ServerSelectionTimeoutError

from app.database import init_indexes
from app.main import create_app
from tests.conftest import KEY, SDP, FakeConnect, FakeLive, call_id, make_settings

H = {"Origin": "http://testserver"}
MISSING = "no-such-default"  # DEFAULT_RESTAURANT_ID that has no record in the seeded database


@pytest.fixture
def unconfigured(make_client):
    """Seeded database (resto-a / resto-b exist) but the implicit default points at nothing."""
    live = FakeLive()
    with make_client(settings=make_settings(default_restaurant_id=MISSING), live_client=live) as c:
        c.live = live
        yield c


@pytest.fixture
def empty_client():
    """A completely empty database: what a fresh deployment looks like."""
    db = mongomock.MongoClient().db
    init_indexes(db)
    app = create_app(make_settings(), db=db, live_client=FakeLive(), sideband_connect=FakeConnect())
    with TestClient(app, base_url="http://testserver") as c:
        c.db = db
        yield c


def call_button(html: str) -> str:
    m = re.search(r'<button[^>]*id="btn-call"[^>]*>', html)
    assert m, "call button not found"
    return m.group(0)


# ===== the setup state itself =====
@pytest.mark.parametrize("path", ["/call", "/menu"])
def test_missing_default_restaurant_renders_a_clear_setup_page(empty_client, path):
    r = empty_client.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Restaurant not configured" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert "script-src 'self'" in r.headers["content-security-policy"]  # same security headers as every page
    assert KEY not in r.text


def test_root_redirect_lands_on_the_setup_page_not_a_json_404(empty_client):
    r = empty_client.get("/", follow_redirects=True)
    assert r.status_code == 200 and "Restaurant not configured" in r.text


def test_call_button_is_disabled_and_no_script_can_start_a_call(empty_client):
    html = empty_client.get("/call").text
    assert "disabled" in call_button(html)
    assert "<script" not in html                       # call.js is not even loaded
    assert "data-restaurant-id" not in html            # nothing a script could post as a restaurant id
    assert 'id="btn-cancel"' not in html and 'id="btn-end"' not in html


def test_menu_setup_page_has_no_items_owner_tools_or_menu_script(empty_client):
    html = empty_client.get("/menu").text
    for forbidden in ("menu-list", "menu.js", "login-form", "Owner tools", "Karahi", "Pizza", "Loading menu"):
        assert forbidden not in html
    assert 'id="btn-call"' not in html                 # the menu page has no call control at all


def test_setup_page_does_not_leak_other_restaurants(unconfigured):
    """resto-a and resto-b exist in the database; the missing default must not borrow their data."""
    for path in ("/call", "/menu"):
        html = unconfigured.get(path).text
        assert "Restaurant not configured" in html
        for leaked in ("Resto A", "Resto B", "resto-a", "resto-b", "Karahi", "Pizza", "Naan", MISSING):
            assert leaked not in html


def test_setup_page_is_not_sticky_once_the_restaurant_exists(empty_client):
    assert "Restaurant not configured" in empty_client.get("/call").text
    empty_client.db.restaurants.insert_one({"_id": "resto-a", "name": "Resto A", "currency": "PKR"})
    r = empty_client.get("/call")
    assert r.status_code == 200 and "Restaurant not configured" not in r.text
    assert "Resto A" in r.text and "disabled" not in call_button(r.text)


def test_setup_state_is_logged_for_operators_without_secrets(unconfigured, caplog):
    with caplog.at_level("WARNING"):
        unconfigured.get("/call")
    assert MISSING in caplog.text and "database" in caplog.text
    assert KEY not in caplog.text and "mongodb://" not in caplog.text


# ===== explicit ids are never softened =====
@pytest.mark.parametrize("path", ["/call", "/menu"])
@pytest.mark.parametrize("rid", ["nope", "%20", "%3Cscript%3Ealert(1)%3C%2Fscript%3E"])
def test_explicit_invalid_restaurant_id_is_still_404(unconfigured, empty_client, path, rid):
    for client in (unconfigured, empty_client):  # whether or not the implicit default exists
        r = client.get(f"{path}?restaurant_id={rid}")
        assert r.status_code == 404
        assert "Restaurant not configured" not in r.text and "<script>alert" not in r.text


def test_explicit_valid_restaurant_still_renders_when_default_is_missing(unconfigured):
    r = unconfigured.get("/call?restaurant_id=resto-b")
    assert r.status_code == 200 and "Resto B" in r.text and "Restaurant not configured" not in r.text
    assert "disabled" not in call_button(r.text)
    menu = unconfigured.get("/menu?restaurant_id=resto-b")
    assert menu.status_code == 200 and "Resto B" in menu.text and "menu.js" in menu.text


# ===== no voice session / order / menu data without a valid restaurant =====
def test_no_voice_session_menu_or_order_without_a_configured_restaurant(unconfigured):
    db = unconfigured.app.state.db
    unconfigured.get("/call")
    unconfigured.get("/menu")
    assert unconfigured.get("/api/menu").status_code == 404                      # API is not softened
    r = unconfigured.post("/api/call/session", json={"sdp": SDP, "client_call_id": call_id()}, headers=H)
    assert r.status_code == 404 and "call_secret" not in r.text
    r = unconfigured.post("/api/call/session", json={"sdp": SDP, "client_call_id": call_id(2), "restaurant_id": "nope"}, headers=H)
    assert r.status_code == 404
    assert unconfigured.live.calls == []                                          # OpenAI never contacted
    assert db.voice_sessions.count_documents({}) == 0 and db.orders.count_documents({}) == 0


# ===== database failures are not "not configured" =====
class _BrokenDB:
    """restaurants.find_one fails like a real unreachable / rejecting MongoDB."""
    def __init__(self, exc):
        def boom(*a, **k):
            raise exc
        self.name = "broken"
        self.restaurants = SimpleNamespace(find_one=boom)


@pytest.mark.parametrize("exc", [ServerSelectionTimeoutError("timed out"), AutoReconnect("lost"),
                                 OperationFailure("not authorized"), RuntimeError("unexpected")])
@pytest.mark.parametrize("path", ["/call", "/menu", "/call?restaurant_id=resto-a", "/api/menu"])
def test_database_failures_are_never_disguised_as_a_missing_restaurant(exc, path):
    app = create_app(make_settings(), db=_BrokenDB(exc), live_client=FakeLive(), sideband_connect=FakeConnect())
    with TestClient(app, base_url="http://testserver", raise_server_exceptions=False) as c:
        r = c.get(path)
    assert r.status_code == 500
    assert "not configured" not in r.text.lower() and "not found" not in r.text.lower()


# ===== configured behaviour is preserved =====
def test_configured_call_page_is_unchanged(make_client):
    with make_client() as c:
        html = c.get("/call").text
    assert 'data-restaurant-id="resto-a"' in html and "/static/js/call.js" in html
    assert "disabled" not in call_button(html)
    assert "Call restaurant" in html and 'id="btn-cancel"' in html and "Restaurant not configured" not in html


def test_configured_menu_page_is_unchanged(make_client):
    with make_client() as c:
        html = c.get("/menu").text
        api = c.get("/api/menu").json()
    assert 'data-restaurant-id="resto-a"' in html and "/static/js/menu.js" in html and "Owner tools" in html
    assert "Restaurant not configured" not in html
    assert {i["name"] for i in api["items"]} == {"Chicken Karahi", "Garlic Naan", "Daal Mash"}  # resto-a only


def test_empty_restaurant_id_param_means_the_default_like_before(make_client):
    with make_client() as c:
        r = c.get("/call?restaurant_id=")
    assert r.status_code == 200 and 'data-restaurant-id="resto-a"' in r.text
