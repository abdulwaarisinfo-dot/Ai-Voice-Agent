import pytest
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from app.models.schemas import CustomerIn, OrderLineIn
from app.security import order_access_token
from app.services import menu_service, order_service as os_
from app.services.order_service import OrderError

A, B, CALL = "resto-a", "resto-b", "c" * 32
CUST = dict(customer_name="Sara", phone="+92 300 1234567", fulfillment="pickup")


def line(item, qty=1, **kw):
    return OrderLineIn(item_id=item, quantity=qty, **kw)


def ready_order(db, call=CALL, **cust):
    os_.set_items(db, A, call, [line("a-karahi", 2), line("a-naan", 1)])
    os_.set_customer(db, A, call, CustomerIn(**{**CUST, **cust}))


# ----- menu + restaurant isolation -----
def test_menu_is_scoped_to_restaurant(db):
    a = menu_service.list_menu(db, menu_service.get_restaurant(db, A))
    b = menu_service.list_menu(db, menu_service.get_restaurant(db, B))
    assert {i["name"] for i in a} == {"Chicken Karahi", "Garlic Naan", "Daal Mash"}
    assert {i["name"] for i in b} == {"Pizza"}


def test_cannot_order_another_restaurants_item(db):
    with pytest.raises(OrderError) as e:
        os_.set_items(db, A, CALL, [line("b-pizza")])
    assert e.value.code == "unknown_item"


# ----- validation -----
@pytest.mark.parametrize("qty", [0, -1, 21])
def test_bad_quantities_rejected(qty):
    with pytest.raises(ValidationError):
        OrderLineIn(item_id="a-naan", quantity=qty)


def test_unavailable_and_unknown_items(db):
    with pytest.raises(OrderError) as e:
        os_.set_items(db, A, CALL, [line("a-daal")])
    assert e.value.code == "item_unavailable"
    with pytest.raises(OrderError) as e:
        os_.set_items(db, A, CALL, [line("nope")])
    assert e.value.code == "unknown_item"


def test_customizations_must_be_listed_options(db):
    with pytest.raises(OrderError) as e:
        os_.set_items(db, A, CALL, [line("a-karahi", customizations=["with ice cream"])])
    assert e.value.code == "invalid_customization"
    o = os_.set_items(db, A, CALL, [line("a-karahi", customizations=["EXTRA SPICY"])])
    assert o["lines"][0]["customizations"] == ["extra spicy"]


@pytest.mark.parametrize("bad", ["abc", "12", "+" + "1" * 30])
def test_bad_phone_rejected(bad):
    with pytest.raises(ValidationError):
        CustomerIn(customer_name="x", phone=bad, fulfillment="pickup")


# ----- server-side pricing -----
def test_totals_come_from_database_prices(db):
    o = os_.set_items(db, A, CALL, [line("a-karahi", 2), line("a-naan", 1)])
    assert o["subtotal_minor"] == 2 * 129900 + 12000 and o["total_minor"] == o["subtotal_minor"]
    o = os_.set_customer(db, A, CALL, CustomerIn(customer_name="S", phone="03001234567", fulfillment="delivery", delivery_address="12 Main St"))
    assert o["delivery_fee_minor"] == 15000 and o["total_minor"] == 2 * 129900 + 12000 + 15000


def test_client_supplied_prices_are_ignored(db):
    forged = OrderLineIn.model_validate({"item_id": "a-naan", "quantity": 1, "unit_price_minor": 1, "price_minor": 1})
    o = os_.set_items(db, A, CALL, [forged])
    assert o["lines"][0]["unit_price_minor"] == 12000


def test_delivery_needs_address_and_must_be_offered(db):
    os_.set_items(db, A, CALL, [line("a-naan")])
    with pytest.raises(OrderError) as e:
        os_.set_customer(db, A, CALL, CustomerIn(customer_name="S", phone="03001234567", fulfillment="delivery"))
    assert e.value.code == "address_required"
    db.restaurants.update_one({"_id": A}, {"$set": {"accepts_delivery": False}})
    with pytest.raises(OrderError) as e:
        os_.set_customer(db, A, CALL, CustomerIn(customer_name="S", phone="03001234567", fulfillment="delivery", delivery_address="x"))
    assert e.value.code == "delivery_unavailable"


# ----- pending vs confirmed -----
def test_pending_until_explicit_confirmation(db):
    ready_order(db)
    assert os_.get_order_for_call(db, A, CALL)["status"] == "pending"
    q = os_.request_confirmation(db, A, CALL)
    assert os_.get_order_for_call(db, A, CALL)["status"] == "awaiting_confirmation"
    assert "Chicken Karahi" in q["readback"] and "Total" in q["readback"]
    res = os_.confirm_order(db, A, CALL, q["quote_hash"])
    order = os_.get_order_for_call(db, A, CALL)
    assert order["status"] == "confirmed" and order["reference"] == res["reference"] and "expires_at" not in order


def test_cannot_confirm_without_readback(db):
    ready_order(db)
    with pytest.raises(OrderError) as e:
        os_.confirm_order(db, A, CALL, "whatever")
    assert e.value.code == "not_read_back"
    assert os_.get_order_for_call(db, A, CALL)["status"] == "pending"


def test_wrong_hash_rejected(db):
    ready_order(db)
    os_.request_confirmation(db, A, CALL)
    with pytest.raises(OrderError) as e:
        os_.confirm_order(db, A, CALL, "0" * 24)
    assert e.value.code == "stale_quote"


def test_edit_after_readback_invalidates_it(db):
    ready_order(db)
    q = os_.request_confirmation(db, A, CALL)
    os_.set_items(db, A, CALL, [line("a-naan", 5)])
    assert os_.get_order_for_call(db, A, CALL)["status"] == "pending"
    with pytest.raises(OrderError):
        os_.confirm_order(db, A, CALL, q["quote_hash"])


def test_price_change_between_readback_and_confirm_blocks_placement(db):
    ready_order(db)
    q = os_.request_confirmation(db, A, CALL)
    db.menu_items.update_one({"_id": "a-naan"}, {"$set": {"price_minor": 99999}})
    with pytest.raises(OrderError) as e:
        os_.confirm_order(db, A, CALL, q["quote_hash"])
    assert e.value.code == "order_changed"
    assert os_.get_order_for_call(db, A, CALL)["status"] == "pending"


def test_item_unavailable_right_before_confirm(db):
    ready_order(db)
    q = os_.request_confirmation(db, A, CALL)
    menu_service.set_availability(db, menu_service.get_restaurant(db, A), "a-karahi", False)
    with pytest.raises(OrderError) as e:
        os_.confirm_order(db, A, CALL, q["quote_hash"])
    assert e.value.code == "item_unavailable" and "not placed" in e.value.message
    assert os_.get_order_for_call(db, A, CALL)["status"] != "confirmed"


def test_incomplete_details_cannot_be_read_back(db):
    os_.set_items(db, A, CALL, [line("a-naan")])
    with pytest.raises(OrderError) as e:
        os_.request_confirmation(db, A, CALL)
    assert e.value.code == "details_missing"


# ----- duplicates -----
def test_double_confirm_is_idempotent(db):
    ready_order(db)
    q = os_.request_confirmation(db, A, CALL)
    first = os_.confirm_order(db, A, CALL, q["quote_hash"])
    second = os_.confirm_order(db, A, CALL, q["quote_hash"])
    assert second["already_confirmed"] and second["reference"] == first["reference"]
    assert db.orders.count_documents({}) == 1


def test_one_order_per_call_even_when_created_twice(db):
    r = menu_service.get_restaurant(db, A)
    o1, o2 = os_._get_or_create(db, r, CALL), os_._get_or_create(db, r, CALL)
    assert o1["_id"] == o2["_id"] and db.orders.count_documents({}) == 1


def test_unique_index_blocks_second_order_for_same_call(db):
    ready_order(db)
    with pytest.raises(DuplicateKeyError):
        db.orders.insert_one({"_id": "x", "reference": "ORD-ZZZZZZZZ", "voice_session_id": CALL, "restaurant_id": A})


def test_confirmed_order_cannot_be_edited(db):
    ready_order(db)
    q = os_.request_confirmation(db, A, CALL)
    os_.confirm_order(db, A, CALL, q["quote_hash"])
    with pytest.raises(OrderError) as e:
        os_.set_items(db, A, CALL, [line("a-naan", 9)])
    assert e.value.code == "order_already_confirmed"


def test_orders_of_two_calls_do_not_mix(db):
    ready_order(db, call="1" * 32)
    os_.set_items(db, A, "2" * 32, [line("a-naan")])
    assert os_.get_order_for_call(db, A, "1" * 32)["_id"] != os_.get_order_for_call(db, A, "2" * 32)["_id"]
    assert os_.get_order_for_call(db, B, "1" * 32) is None  # wrong restaurant sees nothing


# ----- confirmation-page access -----
def test_confirmed_order_access_control(db):
    ready_order(db)
    q = os_.request_confirmation(db, A, CALL)
    ref = os_.confirm_order(db, A, CALL, q["quote_hash"])["reference"]
    oid = os_.get_order_for_call(db, A, CALL)["_id"]
    tok = order_access_token("s3", oid)
    assert os_.get_confirmed_order(db, "s3", ref, tok)["total"].startswith("PKR")
    assert os_.get_confirmed_order(db, "s3", ref, "bad") is None
    assert os_.get_confirmed_order(db, "s3", ref, None) is None
    assert os_.get_confirmed_order(db, "other-secret", ref, tok) is None
    assert os_.get_confirmed_order(db, "s3", "ORD-NOTREAL1", tok) is None
    assert os_.get_confirmed_order(db, "s3", "../etc", tok) is None


def test_unconfirmed_order_is_never_shown(db):
    ready_order(db)
    os_.request_confirmation(db, A, CALL)
    o = os_.get_order_for_call(db, A, CALL)
    assert os_.get_confirmed_order(db, "s3", o["reference"], order_access_token("s3", o["_id"])) is None
