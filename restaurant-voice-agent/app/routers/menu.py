from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..models.schemas import AvailabilityIn, LoginIn, MenuItemIn
from ..security import RateLimiter, client_ip, require_owner, require_same_origin, verify_password
from ..services import menu_service
from .deps import resolve_restaurant

router = APIRouter(prefix="/api")
_DUMMY_HASH = "scrypt$" + "00" * 16 + "$" + "00" * 32  # keeps login timing similar for unknown restaurants


@router.get("/menu")
def get_menu(request: Request, restaurant_id: str | None = Query(None, max_length=64)):
    restaurant = resolve_restaurant(request, restaurant_id)
    items = menu_service.list_menu(request.app.state.db, restaurant)
    return {"restaurant": {"id": restaurant["_id"], "name": restaurant["name"], "currency": restaurant["currency"]}, "items": items}


@router.post("/owner/login", dependencies=[Depends(require_same_origin)])
def login(request: Request, body: LoginIn):
    limiter: RateLimiter = request.app.state.login_limiter
    if not limiter.allow(client_ip(request)):
        raise HTTPException(429, "Too many attempts. Try again later.")
    restaurant = menu_service.get_restaurant(request.app.state.db, body.restaurant_id)
    stored = restaurant.get("owner_password_hash") if restaurant else _DUMMY_HASH
    if not (verify_password(body.password, stored) and restaurant):
        raise HTTPException(401, "Invalid restaurant or password")
    request.session.clear()
    request.session["owner_restaurant_id"] = restaurant["_id"]
    return {"restaurant_id": restaurant["_id"]}


@router.post("/owner/logout", dependencies=[Depends(require_same_origin)])
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/owner/me")
def me(rid: str = Depends(require_owner)):
    return {"restaurant_id": rid}


def _owner_restaurant(request: Request, rid: str) -> dict:
    restaurant = menu_service.get_restaurant(request.app.state.db, rid)
    if restaurant is None:
        raise HTTPException(401, "Owner login required")
    return restaurant


# The restaurant always comes from the signed session, never from the request body or URL.
@router.post("/menu/items", status_code=201, dependencies=[Depends(require_same_origin)])
def add_item(request: Request, body: MenuItemIn, rid: str = Depends(require_owner)):
    return menu_service.create_item(request.app.state.db, _owner_restaurant(request, rid), body)


@router.put("/menu/items/{item_id}", dependencies=[Depends(require_same_origin)])
def edit_item(request: Request, item_id: str, body: MenuItemIn, rid: str = Depends(require_owner)):
    item = menu_service.update_item(request.app.state.db, _owner_restaurant(request, rid), item_id, body)
    if item is None:
        raise HTTPException(404, "Item not found")
    return item


@router.patch("/menu/items/{item_id}/availability", dependencies=[Depends(require_same_origin)])
def availability(request: Request, item_id: str, body: AvailabilityIn, rid: str = Depends(require_owner)):
    item = menu_service.set_availability(request.app.state.db, _owner_restaurant(request, rid), item_id, body.available)
    if item is None:
        raise HTTPException(404, "Item not found")
    return item
