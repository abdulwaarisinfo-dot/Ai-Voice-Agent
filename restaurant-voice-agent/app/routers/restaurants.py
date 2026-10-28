"""Restaurant management.

Authorisation model (built on the existing session mechanism, nothing is trusted from the browser):
  * owner  - session[owner_restaurant_id], set by /api/owner/login. May read/update/archive ONLY that restaurant.
  * admin  - session[admin], set by /api/admin/login with ADMIN_PASSWORD (optional; unset = disabled).
             Only an admin may create restaurants or list all of them.
Deletion is an ARCHIVE (status="archived"): orders, voice_sessions and menu items are kept untouched, the
restaurant disappears from every public/owner path, and its live calls are closed.
"""
import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pymongo.errors import DuplicateKeyError

from ..models.schemas import AdminLoginIn, RestaurantCreateIn, RestaurantDeleteIn, RestaurantUpdateIn
from ..security import client_ip, hash_password, require_same_origin, verify_password
from ..services import menu_service

router = APIRouter(prefix="/api", dependencies=[Depends(require_same_origin)])


def _public(doc: dict) -> dict:
    """Explicit allow-list: password hashes and any other internal field can never leak."""
    return {"id": doc["_id"], "name": doc["name"], "currency": doc["currency"],
            "delivery_fee_minor": doc.get("delivery_fee_minor", 0),
            "accepts_pickup": doc.get("accepts_pickup", True), "accepts_delivery": doc.get("accepts_delivery", True),
            "status": doc.get("status", "active")}


def _limit(request: Request) -> None:
    if not request.app.state.login_limiter.allow(client_ip(request)):
        raise HTTPException(429, "Too many attempts. Try again later.")


def _actor(request: Request) -> tuple[str, str | None]:
    """('admin', None) or ('owner', restaurant_id). The role comes only from the signed session."""
    if request.session.get("admin") is True and request.app.state.settings.admin_password:
        return "admin", None
    rid = request.session.get("owner_restaurant_id")
    if rid:
        return "owner", rid
    raise HTTPException(401, "Login required")


def _require_admin(request: Request) -> None:
    if _actor(request)[0] != "admin":
        raise HTTPException(403, "Administrator access required")


def _authorized(request: Request, restaurant_id: str, include_archived: bool = False) -> dict:
    """The restaurant, only if this session may manage it. Foreign or unknown ids look identical (404)."""
    role, own = _actor(request)
    restaurant = menu_service.get_restaurant(request.app.state.db, restaurant_id, include_archived=include_archived)
    if restaurant is None or (role == "owner" and own != restaurant["_id"]):
        raise HTTPException(404, "Restaurant not found")
    return restaurant


@router.post("/admin/login")
def admin_login(request: Request, body: AdminLoginIn):
    _limit(request)
    configured = request.app.state.settings.admin_password
    if not configured:
        raise HTTPException(403, "Administrator access is not enabled")
    if not hmac.compare_digest(body.password.encode(), configured.encode()):
        raise HTTPException(401, "Invalid password")
    request.session.clear()
    request.session["admin"] = True
    return {"ok": True}


@router.post("/restaurants", status_code=201)
def create_restaurant(request: Request, body: RestaurantCreateIn):
    _require_admin(request)
    now = datetime.now(timezone.utc)
    doc = {"_id": body.id, "name": body.name, "currency": body.currency, "delivery_fee_minor": body.delivery_fee_minor,
           "accepts_pickup": body.accepts_pickup, "accepts_delivery": body.accepts_delivery,
           "owner_password_hash": hash_password(body.owner_password), "status": "active",
           "created_at": now, "updated_at": now}
    try:
        request.app.state.db.restaurants.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "A restaurant with this id already exists")  # archived ids stay reserved too
    return _public(doc)


@router.get("/restaurants")
def list_restaurants(request: Request, include_archived: bool = Query(False)):
    role, own = _actor(request)
    db = request.app.state.db
    if role == "owner":
        found = menu_service.get_restaurant(db, own)
        return {"restaurants": [_public(found)] if found else []}
    query = {} if include_archived else {"status": {"$ne": menu_service.ARCHIVED}}
    return {"restaurants": [_public(d) for d in db.restaurants.find(query).sort("_id", 1)]}


@router.get("/restaurants/{restaurant_id}")
def get_restaurant(request: Request, restaurant_id: str):
    return _public(_authorized(request, restaurant_id))


@router.patch("/restaurants/{restaurant_id}")
def update_restaurant(request: Request, restaurant_id: str, body: RestaurantUpdateIn):
    current = _authorized(request, restaurant_id)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(422, "No editable fields supplied")
    merged = {**current, **changes}
    if not (merged.get("accepts_pickup", True) or merged.get("accepts_delivery", True)):
        raise HTTPException(422, "A restaurant must accept pickup or delivery")
    updated = request.app.state.db.restaurants.find_one_and_update(
        {"_id": current["_id"], "status": {"$ne": menu_service.ARCHIVED}},
        {"$set": {**changes, "updated_at": datetime.now(timezone.utc)}}, return_document=True)
    if updated is None:
        raise HTTPException(404, "Restaurant not found")
    return _public(updated)


# async on purpose: the archive write and the termination of live calls run in one event-loop step, so a call
# cannot slip in between "archived" and "active calls closed".
@router.delete("/restaurants/{restaurant_id}")
async def archive_restaurant(request: Request, restaurant_id: str, body: RestaurantDeleteIn):
    role, _ = _actor(request)
    restaurant = _authorized(request, restaurant_id)
    _limit(request)
    if body.confirm_id != restaurant["_id"]:
        raise HTTPException(422, "confirm_id does not match the restaurant id")
    if role == "admin":
        ok = hmac.compare_digest(body.password.encode(), request.app.state.settings.admin_password.encode())
    else:
        ok = verify_password(body.password, restaurant.get("owner_password_hash"))
    if not ok:
        raise HTTPException(401, "Password incorrect")
    now = datetime.now(timezone.utc)
    res = request.app.state.db.restaurants.update_one(
        {"_id": restaurant["_id"], "status": {"$ne": menu_service.ARCHIVED}},
        {"$set": {"status": menu_service.ARCHIVED, "archived_at": now, "updated_at": now}})
    if res.matched_count == 0:
        raise HTTPException(404, "Restaurant not found")
    closed = request.app.state.live.terminate_restaurant(restaurant["_id"])
    if role == "owner":
        request.session.clear()
    return {"id": restaurant["_id"], "status": "archived", "active_calls_closed": closed}
