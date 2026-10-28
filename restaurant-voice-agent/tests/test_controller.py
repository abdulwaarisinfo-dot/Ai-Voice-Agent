"""The tool layer the model talks to. The model can only act through these calls."""
import json

from app.services.live_session import CallController
from app.services import order_service as os_
from tests.conftest import tool_event

A, CALL = "resto-a", "d" * 32


def run(ctl, cid, name, args):
    out = ctl.handle_event(tool_event(cid, name, args))
    assert [o["type"] for o in out] == ["response.item.create", "response.create"]
    assert out[0]["item"]["call_id"] == cid
    return json.loads(out[0]["item"]["output"])


def fill(ctl):
    assert run(ctl, "t1", "set_order_items", {"items": [{"item_id": "a-karahi", "quantity": 1}]})["ok"]
    assert run(ctl, "t2", "set_customer_details", {"customer_name": "Sara", "phone": "03001234567", "fulfillment": "pickup"})["ok"]


def test_get_menu_returns_only_this_restaurants_verified_items(db):
    ctl = CallController(db, A, CALL)
    res = run(ctl, "m1", "get_menu", {})
    names = {i["name"] for i in res["items"]}
    assert "Chicken Karahi" in names and "Pizza" not in names
    assert next(i for i in res["items"] if i["name"] == "Daal Mash")["available"] is False


def test_model_cannot_redirect_to_another_restaurant(db):
    ctl = CallController(db, A, CALL)
    res = run(ctl, "x1", "set_order_items", {"restaurant_id": "resto-b", "items": [{"item_id": "b-pizza", "quantity": 1}]})
    assert res["ok"] is False and res["error_code"] == "unknown_item"
    assert db.orders.count_documents({"restaurant_id": "resto-b"}) == 0


def test_model_cannot_set_price_or_total(db):
    ctl = CallController(db, A, CALL)
    res = run(ctl, "p1", "set_order_items", {"items": [{"item_id": "a-naan", "quantity": 2, "unit_price_minor": 1}], "total_minor": 1})
    assert res["ok"] and res["order"]["total"] == "PKR 240.00"


def test_full_confirmation_requires_customer_reply_after_readback(db):
    ctl = CallController(db, A, CALL)
    fill(ctl)
    q = run(ctl, "r1", "request_confirmation", {})
    assert q["ok"] and q["status"] == "awaiting_confirmation"
    early = run(ctl, "c1", "confirm_order", {"quote_hash": q["quote_hash"], "customer_confirmed": True})
    assert early["ok"] is False and early["error_code"] == "customer_has_not_replied"
    assert os_.get_order_for_call(db, A, CALL)["status"] == "awaiting_confirmation"
    ctl.handle_event({"type": "session.input_transcript.delta", "delta": "yes, confirm"})
    done = run(ctl, "c2", "confirm_order", {"quote_hash": q["quote_hash"], "customer_confirmed": True})
    assert done["ok"] and done["confirmed"] and done["reference"].startswith("ORD-")


def test_customer_confirmed_false_never_places_order(db):
    ctl = CallController(db, A, CALL)
    fill(ctl)
    q = run(ctl, "r1", "request_confirmation", {})
    ctl.handle_event({"type": "session.input_transcript.delta", "delta": "no wait"})
    res = run(ctl, "c1", "confirm_order", {"quote_hash": q["quote_hash"], "customer_confirmed": False})
    assert res["ok"] is False and os_.get_order_for_call(db, A, CALL)["status"] != "confirmed"


def test_editing_after_readback_requires_a_new_readback(db):
    ctl = CallController(db, A, CALL)
    fill(ctl)
    q = run(ctl, "r1", "request_confirmation", {})
    ctl.handle_event({"type": "session.input_transcript.delta", "delta": "yes"})
    run(ctl, "e1", "set_order_items", {"items": [{"item_id": "a-karahi", "quantity": 3}]})
    res = run(ctl, "c1", "confirm_order", {"quote_hash": q["quote_hash"], "customer_confirmed": True})
    assert res["ok"] is False


def test_repeated_tool_call_id_does_not_execute_twice(db):
    ctl = CallController(db, A, CALL)
    fill(ctl)
    q = run(ctl, "r1", "request_confirmation", {})
    ctl.handle_event({"type": "session.input_transcript.delta", "delta": "yes"})
    first = run(ctl, "dup", "confirm_order", {"quote_hash": q["quote_hash"], "customer_confirmed": True})
    again = run(ctl, "dup", "confirm_order", {"quote_hash": q["quote_hash"], "customer_confirmed": True})
    assert first == again and db.orders.count_documents({}) == 1


def test_bad_input_and_unknown_tools_fail_safely(db):
    ctl = CallController(db, A, CALL)
    out = ctl.handle_event({"type": "response.event", "event": {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "j1", "name": "set_order_items", "arguments": "{not json"}}})
    assert json.loads(out[0]["item"]["output"])["error_code"] == "invalid_input"
    assert run(ctl, "j2", "set_order_items", {"items": [{"item_id": "a-naan", "quantity": 500}]})["error_code"] == "invalid_input"
    assert run(ctl, "j3", "drop_database", {})["error_code"] == "unknown_tool"
    assert run(ctl, "j4", "set_customer_details", {"customer_name": "x", "phone": "1", "fulfillment": "pickup"})["ok"] is False


def test_internal_errors_never_claim_success(db, monkeypatch):
    ctl = CallController(db, A, CALL)
    monkeypatch.setattr(os_, "set_items", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    res = run(ctl, "i1", "set_order_items", {"items": [{"item_id": "a-naan", "quantity": 1}]})
    assert res["ok"] is False and res["error_code"] == "internal_error" and "db down" not in json.dumps(res)


def test_session_closed_does_not_confirm_or_discard_pending_order(db):
    ctl = CallController(db, A, CALL)
    fill(ctl)
    ctl.handle_event({"type": "session.closed", "usage": {"seconds": 12}})
    assert ctl.closed and ctl.usage == {"seconds": 12}
    assert os_.get_order_for_call(db, A, CALL)["status"] == "pending"
