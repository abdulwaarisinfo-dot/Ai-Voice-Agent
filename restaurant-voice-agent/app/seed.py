"""Optional demo data:  python -m app.seed
Creates ONE sample restaurant and a few sample dishes so you can try the flow.
These are placeholder records for development - replace them with the real menu via the menu page."""
from datetime import datetime, timezone

from .config import get_settings
from .database import connect, init_indexes
from .security import hash_password

SAMPLE = [
    ("Chicken Karahi", "Tomato-based karahi with green chilli and ginger", "Mains", 129900, ["extra spicy", "mild"]),
    ("Daal Mash", "Slow-cooked white lentils with butter", "Mains", 59900, []),
    ("Garlic Naan", "Tandoor-baked bread with garlic", "Breads", 12000, []),
    ("Mint Lassi", "Yoghurt drink with mint", "Drinks", 35000, ["no sugar"]),
]


def main() -> None:
    s = get_settings()
    if not s.owner_password:
        raise SystemExit("Set OWNER_PASSWORD in your environment/.env first (used for the demo owner login).")
    db = connect(s)
    init_indexes(db)
    now = datetime.now(timezone.utc)
    db.restaurants.update_one({"_id": s.default_restaurant_id}, {"$set": {
        "name": "Demo Restaurant", "currency": "PKR", "delivery_fee_minor": 15000,
        "accepts_pickup": True, "accepts_delivery": True, "owner_password_hash": hash_password(s.owner_password)}}, upsert=True)
    if db.menu_items.count_documents({"restaurant_id": s.default_restaurant_id}) == 0:
        for i, (name, desc, cat, price, opts) in enumerate(SAMPLE):
            db.menu_items.insert_one({"_id": f"sample-{i + 1}", "restaurant_id": s.default_restaurant_id, "name": name,
                                      "description": desc, "category": cat, "price_minor": price, "available": True,
                                      "options": opts, "created_at": now, "updated_at": now})
    print(f"Seeded restaurant '{s.default_restaurant_id}'.")


if __name__ == "__main__":
    main()
