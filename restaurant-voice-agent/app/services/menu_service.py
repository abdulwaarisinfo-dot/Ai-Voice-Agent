"""Menu queries. Every query is scoped by restaurant_id."""
import uuid
from datetime import datetime, timezone

from ..money import format_money
from ..models.schemas import MenuItemIn


def get_restaurant(db, restaurant_id: str) -> dict | None:
    return db.restaurants.find_one({"_id": restaurant_id})


def public_item(doc: dict, currency: str) -> dict:
    return {
        "id": doc["_id"],
        "name": doc["name"],
        "description": doc.get("description", ""),
        "category": doc["category"],
        "price_minor": doc["price_minor"],
        "price": format_money(doc["price_minor"], currency),
        "available": bool(doc["available"]),
        "options": doc.get("options", []),
    }


def list_menu(db, restaurant: dict, category: str | None = None) -> list[dict]:
    query = {"restaurant_id": restaurant["_id"]}
    if category:
        query["category"] = category
    docs = db.menu_items.find(query).sort([("category", 1), ("name", 1)])
    return [public_item(d, restaurant["currency"]) for d in docs]


def create_item(db, restaurant: dict, data: MenuItemIn) -> dict:
    now = datetime.now(timezone.utc)
    doc = {"_id": uuid.uuid4().hex, "restaurant_id": restaurant["_id"], **data.model_dump(),
           "created_at": now, "updated_at": now}
    db.menu_items.insert_one(doc)
    return public_item(doc, restaurant["currency"])


def update_item(db, restaurant: dict, item_id: str, data: MenuItemIn) -> dict | None:
    doc = db.menu_items.find_one_and_update(
        {"_id": item_id, "restaurant_id": restaurant["_id"]},
        {"$set": {**data.model_dump(), "updated_at": datetime.now(timezone.utc)}},
        return_document=True,
    )
    return public_item(doc, restaurant["currency"]) if doc else None


def set_availability(db, restaurant: dict, item_id: str, available: bool) -> dict | None:
    doc = db.menu_items.find_one_and_update(
        {"_id": item_id, "restaurant_id": restaurant["_id"]},
        {"$set": {"available": available, "updated_at": datetime.now(timezone.utc)}},
        return_document=True,
    )
    return public_item(doc, restaurant["currency"]) if doc else None
