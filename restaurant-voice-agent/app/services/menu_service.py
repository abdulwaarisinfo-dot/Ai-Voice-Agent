"""Menu queries. Every query is scoped by restaurant_id."""
import re
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher

from ..money import format_money, spoken_money
from ..models.schemas import MenuItemIn


ARCHIVED = "archived"


def is_active(restaurant: dict | None) -> bool:
    """Records created before the `status` field existed have none: they are active."""
    return restaurant is not None and restaurant.get("status") != ARCHIVED


def get_restaurant(db, restaurant_id: str, include_archived: bool = False) -> dict | None:
    """Archived restaurants are invisible by default, so they can take no new calls, logins or orders."""
    doc = db.restaurants.find_one({"_id": restaurant_id})
    return doc if include_archived or is_active(doc) else None


def public_item(doc: dict, currency: str) -> dict:
    return {
        "id": doc["_id"],
        "name": doc["name"],
        "description": doc.get("description", ""),
        "category": doc["category"],
        "price_minor": doc["price_minor"],
        "price": format_money(doc["price_minor"], currency),
        "price_spoken": spoken_money(doc["price_minor"], currency),
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


def delete_item(db, restaurant: dict, item_id: str) -> bool:
    """Hard-delete one menu item. Orders keep their own name/price snapshot, so history is unaffected;
    a pending order that still references the item fails re-pricing and cannot be confirmed."""
    res = db.menu_items.delete_one({"_id": item_id, "restaurant_id": restaurant["_id"]})
    return res.deleted_count == 1


# ---------- menu lookup for the voice agent ----------
# Filler words people say around a dish name (English + Roman Urdu). They must not count as a match.
_STOP = {"the", "a", "an", "with", "and", "of", "for", "please", "do", "you", "have", "has", "is", "are", "there", "any",
         "me", "i", "we", "want", "need", "get", "price", "rate", "cost", "how", "much", "what", "whats", "tell", "about",
         "one", "some", "order", "ka", "ki", "ke", "ko", "hai", "hain", "kya", "aap", "ap", "ke", "paas", "kitne", "kitna",
         "kitni", "chahiye", "chahie", "dena", "dedo", "mujhe", "hamein", "bata", "batao", "bataye", "menu", "mein", "main"}


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", (text or "").lower(), flags=re.UNICODE) if t not in _STOP]


def _fuzzy_token(a: str, b: str) -> bool:
    return a == b or (len(a) >= 3 and len(b) >= 3 and SequenceMatcher(None, a, b).ratio() >= 0.8)


def search_menu(db, restaurant: dict, query: str, limit: int = 5) -> dict:
    """Deterministic, server-side check of 'is this dish on the menu?'. The model never decides this itself.

    match = exact   : the dish name equals the query
            partial : every word of the query is in the dish name/category/description
            similar : spelling/word close to a dish name (e.g. 'karhai' -> 'Chicken Karahi'); customer must confirm
            none    : not on the menu
    """
    items = list_menu(db, restaurant)
    categories = sorted({i["category"] for i in items})
    base = {"query": query, "categories": categories, "item_count": len(items)}
    if not items:
        return {**base, "match": "none", "matches": [], "menu_empty": True}
    q = _tokens(query)
    if not q:
        return {**base, "match": "none", "matches": [], "menu_empty": False, "query_unclear": True}
    exact, partial, similar = [], [], []
    for it in items:
        name_t = _tokens(it["name"])
        all_t = name_t + _tokens(it["category"]) + _tokens(it["description"])
        if q == name_t:
            exact.append(it)
        elif all(any(t == w for w in all_t) for t in q):
            partial.append(it)
        elif any(_fuzzy_token(t, w) for t in q for w in name_t):
            similar.append(it)
    if exact:
        match, found = "exact", exact
    elif partial:
        match, found = "partial", partial
    elif similar:
        match, found = "similar", similar
    else:
        match, found = "none", []
    keep = ("id", "name", "description", "category", "price", "price_spoken", "available", "options")
    return {**base, "match": match, "menu_empty": False,
            "matches": [{k: i[k] for k in keep} for i in found[:limit]]}
