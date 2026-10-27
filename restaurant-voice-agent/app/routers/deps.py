from fastapi import HTTPException, Request

from ..services import menu_service


def resolve_restaurant(request: Request, restaurant_id: str | None) -> dict:
    """Validate the restaurant context against the database; unknown ids are a 404."""
    rid = (restaurant_id or request.app.state.settings.default_restaurant_id).strip()
    restaurant = menu_service.get_restaurant(request.app.state.db, rid)
    if restaurant is None:
        raise HTTPException(404, "Restaurant not found")
    return restaurant
