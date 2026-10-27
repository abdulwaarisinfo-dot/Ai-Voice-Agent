"""Trusted order logic. The language model never writes to the database directly:
it calls tools (see live_session.py) which call these functions.

Lifecycle:  pending -> awaiting_confirmation -> confirmed
  * any change to items/customer details sends the order back to `pending`
  * request_confirmation() re-prices from the DB and fixes a `quote_hash`
  * confirm_order() only succeeds from `awaiting_confirmation` with the same quote_hash,
    re-checks availability, and is idempotent (second call returns the same order)
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from ..money import format_money
from ..models.schemas import CustomerIn, OrderLineIn
from ..security import order_access_token
from .menu_service import get_restaurant

PENDING_TTL = timedelta(hours=24)
REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class OrderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_reference() -> str:
    return "ORD-" + "".join(secrets.choice(REF_ALPHABET) for _ in range(8))


def validation_message(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:4])


# ---------- pricing ----------
def price_lines(db, restaurant: dict, lines: list[OrderLineIn]) -> list[dict]:
    """Look every item up in the DB for THIS restaurant. Prices come only from here."""
    if not lines:
        raise OrderError("empty_order", "The order has no items yet.")
    ids = list({line.item_id for line in lines})
    docs = {d["_id"]: d for d in db.menu_items.find({"restaurant_id": restaurant["_id"], "_id": {"$in": ids}})}
    priced = []
    for line in lines:
        doc = docs.get(line.item_id)
        if doc is None:
            raise OrderError("unknown_item", f"Item '{line.item_id}' is not on this restaurant's menu. Use get_menu to find valid item_ids.")
        if not doc["available"]:
            raise OrderError("item_unavailable", f"{doc['name']} is currently unavailable.")
        allowed = {o.lower(): o for o in doc.get("options", [])}
        chosen = []
        for c in line.customizations:
            canonical = allowed.get(c.strip().lower())
            if canonical is None:
                raise OrderError("invalid_customization",
                                 f"'{c}' is not an option for {doc['name']}. Allowed: {', '.join(allowed.values()) or 'none'}.")
            chosen.append(canonical)
        priced.append({
            "item_id": doc["_id"], "name": doc["name"], "quantity": line.quantity,
            "customizations": sorted(set(chosen)), "unit_price_minor": doc["price_minor"],
            "line_total_minor": doc["price_minor"] * line.quantity,
        })
    return priced


def compute_totals(restaurant: dict, lines: list[dict], fulfillment: str | None) -> dict:
    subtotal = sum(line["line_total_minor"] for line in lines)
    fee = restaurant.get("delivery_fee_minor", 0) if fulfillment == "delivery" else 0
    return {"subtotal_minor": subtotal, "delivery_fee_minor": fee, "total_minor": subtotal + fee}


def quote_hash(order: dict) -> str:
    payload = {
        "lines": [[l["item_id"], l["quantity"], l["customizations"], l["unit_price_minor"]] for l in order["lines"]],
        "fulfillment": order.get("fulfillment"), "address": order.get("delivery_address"),
        "fee": order["delivery_fee_minor"], "total": order["total_minor"],
        "customer": order.get("customer"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]


# ---------- views ----------
def order_view(order: dict) -> dict:
    cur = order["currency"]
    return {
        "reference": order["reference"], "status": order["status"], "currency": cur,
        "fulfillment": order.get("fulfillment"), "delivery_address": order.get("delivery_address"),
        "customer_name": (order.get("customer") or {}).get("name"),
        "lines": [{**l, "unit_price": format_money(l["unit_price_minor"], cur),
                   "line_total": format_money(l["line_total_minor"], cur)} for l in order.get("lines", [])],
        "subtotal": format_money(order.get("subtotal_minor", 0), cur),
        "delivery_fee": format_money(order.get("delivery_fee_minor", 0), cur),
        "total": format_money(order.get("total_minor", 0), cur),
        "confirmed_at": order.get("confirmed_at"),
    }


def readback_text(order: dict) -> str:
    cur = order["currency"]
    parts = []
    for l in order["lines"]:
        extra = f" ({', '.join(l['customizations'])})" if l["customizations"] else ""
        parts.append(f"{l['quantity']} x {l['name']}{extra}")
    where = "delivery to " + order["delivery_address"] if order["fulfillment"] == "delivery" else "pickup"
    fee = f", delivery fee {format_money(order['delivery_fee_minor'], cur)}" if order["delivery_fee_minor"] else ""
    return (f"{'; '.join(parts)}. {where.capitalize()}{fee}. Total {format_money(order['total_minor'], cur)}. "
            f"Name {order['customer']['name']}, phone {order['customer']['phone']}.")


# ---------- persistence ----------
def get_order_for_call(db, restaurant_id: str, call_id: str) -> dict | None:
    return db.orders.find_one({"voice_session_id": call_id, "restaurant_id": restaurant_id})


def _get_or_create(db, restaurant: dict, call_id: str) -> dict:
    existing = get_order_for_call(db, restaurant["_id"], call_id)
    if existing:
        return existing
    for _ in range(5):
        now = _now()
        doc = {"_id": secrets.token_hex(12), "reference": new_reference(), "restaurant_id": restaurant["_id"],
               "voice_session_id": call_id, "status": "pending", "lines": [], "subtotal_minor": 0,
               "delivery_fee_minor": 0, "total_minor": 0, "currency": restaurant["currency"],
               "fulfillment": None, "customer": None, "delivery_address": None, "quote_hash": None,
               "created_at": now, "updated_at": now, "expires_at": now + PENDING_TTL}
        try:
            db.orders.insert_one(doc)
            return doc
        except DuplicateKeyError:
            existing = get_order_for_call(db, restaurant["_id"], call_id)
            if existing:  # lost a race with ourselves: same call, same order
                return existing
    raise OrderError("internal_error", "Could not create the order.")


def _reload_lines(order: dict) -> list[OrderLineIn]:
    return [OrderLineIn(item_id=l["item_id"], quantity=l["quantity"], customizations=l["customizations"]) for l in order["lines"]]


def set_items(db, restaurant_id: str, call_id: str, lines: list[OrderLineIn]) -> dict:
    restaurant = get_restaurant(db, restaurant_id)
    order = _get_or_create(db, restaurant, call_id)
    if order["status"] == "confirmed":
        raise OrderError("order_already_confirmed", "This order is already confirmed and cannot be changed. A new call is needed for another order.")
    priced = price_lines(db, restaurant, lines)
    totals = compute_totals(restaurant, priced, order.get("fulfillment"))
    return _save(db, order, {"lines": priced, **totals})


def set_customer(db, restaurant_id: str, call_id: str, data: CustomerIn) -> dict:
    restaurant = get_restaurant(db, restaurant_id)
    order = _get_or_create(db, restaurant, call_id)
    if order["status"] == "confirmed":
        raise OrderError("order_already_confirmed", "This order is already confirmed and cannot be changed.")
    if data.fulfillment == "delivery" and not restaurant.get("accepts_delivery", True):
        raise OrderError("delivery_unavailable", "This restaurant does not offer delivery.")
    if data.fulfillment == "pickup" and not restaurant.get("accepts_pickup", True):
        raise OrderError("pickup_unavailable", "This restaurant does not offer pickup.")
    if data.fulfillment == "delivery" and not data.delivery_address:
        raise OrderError("address_required", "A delivery address is required for delivery.")
    totals = compute_totals(restaurant, order["lines"], data.fulfillment)
    return _save(db, order, {
        "customer": {"name": data.customer_name, "phone": data.phone}, "fulfillment": data.fulfillment,
        "delivery_address": data.delivery_address if data.fulfillment == "delivery" else None, **totals})


def _save(db, order: dict, changes: dict) -> dict:
    """Any edit invalidates a previous read-back."""
    changes = {**changes, "status": "pending", "quote_hash": None, "updated_at": _now()}
    updated = db.orders.find_one_and_update(
        {"_id": order["_id"], "restaurant_id": order["restaurant_id"], "status": {"$ne": "confirmed"}},
        {"$set": changes}, return_document=True)
    if updated is None:
        raise OrderError("order_already_confirmed", "This order is already confirmed and cannot be changed.")
    return updated


def request_confirmation(db, restaurant_id: str, call_id: str) -> dict:
    """Step 1: re-price from the DB, verify completeness, fix the quote the customer will hear."""
    restaurant = get_restaurant(db, restaurant_id)
    order = get_order_for_call(db, restaurant_id, call_id)
    if order is None or not order["lines"]:
        raise OrderError("empty_order", "No items have been added yet.")
    if order["status"] == "confirmed":
        raise OrderError("order_already_confirmed", "This order is already confirmed.")
    if not order.get("customer") or not order.get("fulfillment"):
        raise OrderError("details_missing", "Customer name, phone and pickup/delivery choice are required first.")
    if order["fulfillment"] == "delivery" and not order.get("delivery_address"):
        raise OrderError("address_required", "A delivery address is required.")
    priced = price_lines(db, restaurant, _reload_lines(order))  # re-verify availability + prices
    fresh = {**order, "lines": priced, **compute_totals(restaurant, priced, order["fulfillment"])}
    qh = quote_hash(fresh)
    updated = db.orders.find_one_and_update(
        {"_id": order["_id"], "restaurant_id": restaurant_id, "status": {"$in": ["pending", "awaiting_confirmation"]}},
        {"$set": {"lines": priced, "subtotal_minor": fresh["subtotal_minor"], "delivery_fee_minor": fresh["delivery_fee_minor"],
                  "total_minor": fresh["total_minor"], "quote_hash": qh, "status": "awaiting_confirmation", "updated_at": _now()}},
        return_document=True)
    if updated is None:
        raise OrderError("order_already_confirmed", "This order is already confirmed.")
    return {"status": "awaiting_confirmation", "quote_hash": qh, "readback": readback_text(updated),
            "total": format_money(updated["total_minor"], updated["currency"]),
            "instruction": "Read this back to the customer and ask for a clear yes. Do not call confirm_order until they answer."}


def confirm_order(db, restaurant_id: str, call_id: str, presented_hash: str) -> dict:
    """Step 2: the only path to `confirmed`."""
    restaurant = get_restaurant(db, restaurant_id)
    order = get_order_for_call(db, restaurant_id, call_id)
    if order is None:
        raise OrderError("no_order", "There is no order to confirm.")
    if order["status"] == "confirmed":
        return {"already_confirmed": True, "reference": order["reference"], "total": format_money(order["total_minor"], order["currency"])}
    if order["status"] != "awaiting_confirmation" or not order.get("quote_hash"):
        raise OrderError("not_read_back", "Call request_confirmation and read the order back before confirming.")
    if presented_hash != order["quote_hash"]:
        raise OrderError("stale_quote", "The order changed since the read-back. Call request_confirmation again.")
    try:
        priced = price_lines(db, restaurant, _reload_lines(order))  # availability immediately before commit
    except OrderError as exc:
        db.orders.update_one({"_id": order["_id"], "status": "awaiting_confirmation"}, {"$set": {"status": "pending", "quote_hash": None}})
        raise OrderError(exc.code, exc.message + " The order was not placed. Tell the customer and fix the order.")
    fresh = {**order, "lines": priced, **compute_totals(restaurant, priced, order["fulfillment"])}
    if quote_hash(fresh) != order["quote_hash"]:
        db.orders.update_one({"_id": order["_id"], "status": "awaiting_confirmation"}, {"$set": {"status": "pending", "quote_hash": None}})
        raise OrderError("order_changed", "Prices or availability changed. The order was not placed; read the updated order back.")
    now = _now()
    done = db.orders.find_one_and_update(  # atomic: only one caller can win this transition
        {"_id": order["_id"], "restaurant_id": restaurant_id, "status": "awaiting_confirmation", "quote_hash": order["quote_hash"]},
        {"$set": {"status": "confirmed", "confirmed_at": now, "updated_at": now}, "$unset": {"expires_at": ""}},
        return_document=True)
    if done is None:  # concurrent duplicate: report the winner's result
        again = get_order_for_call(db, restaurant_id, call_id)
        if again and again["status"] == "confirmed":
            return {"already_confirmed": True, "reference": again["reference"], "total": format_money(again["total_minor"], again["currency"])}
        raise OrderError("confirm_failed", "The order could not be confirmed. It was not placed.")
    return {"confirmed": True, "reference": done["reference"], "total": format_money(done["total_minor"], done["currency"])}


def summary(db, restaurant_id: str, call_id: str) -> dict:
    order = get_order_for_call(db, restaurant_id, call_id)
    if order is None:
        return {"status": "none", "lines": [], "total": None}
    view = order_view(order)
    return {"status": view["status"], "lines": [{k: l[k] for k in ("item_id", "name", "quantity", "customizations", "unit_price", "line_total")} for l in view["lines"]],
            "subtotal": view["subtotal"], "delivery_fee": view["delivery_fee"], "total": view["total"],
            "fulfillment": view["fulfillment"], "delivery_address": view["delivery_address"], "customer_name": view["customer_name"]}


# ---------- customer-facing retrieval ----------
def get_confirmed_order(db, secret: str, reference: str, token: str | None) -> dict | None:
    """Only returns an order that is persisted as confirmed AND whose access token matches."""
    if not token or not reference.startswith("ORD-") or len(reference) != 12:
        return None
    order = db.orders.find_one({"reference": reference})
    if order is None or order["status"] != "confirmed":
        return None
    import hmac
    if not hmac.compare_digest(token, order_access_token(secret, order["_id"])):
        return None
    restaurant = get_restaurant(db, order["restaurant_id"])
    view = order_view(order)
    view["restaurant_name"] = restaurant["name"] if restaurant else "Restaurant"
    view["restaurant_id"] = order["restaurant_id"]
    return view
