"""HTML pages. Opening any of them never contacts OpenAI or creates a session."""
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..services import order_service
from .deps import resolve_restaurant

router = APIRouter()


def _render(request: Request, name: str, status: int = 200, **ctx):
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


@router.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/call")


@router.get("/call", response_class=HTMLResponse)
def call_page(request: Request, restaurant_id: str | None = Query(None, max_length=64)):
    restaurant = resolve_restaurant(request, restaurant_id)
    return _render(request, "call.html", restaurant=restaurant, page="call")


@router.get("/menu", response_class=HTMLResponse)
def menu_page(request: Request, restaurant_id: str | None = Query(None, max_length=64)):
    restaurant = resolve_restaurant(request, restaurant_id)
    return _render(request, "menu.html", restaurant=restaurant, page="menu")


@router.get("/confirmation/{reference}", response_class=HTMLResponse)
def confirmation_page(request: Request, reference: str, t: str | None = Query(None, max_length=80)):
    """Shows a persisted, confirmed order only. Visiting the URL proves nothing by itself."""
    app = request.app
    order = order_service.get_confirmed_order(app.state.db, app.state.secret, reference, t)
    if order is None:
        return _render(request, "confirmation.html", status=404, order=None, page="confirmation",
                       restaurant={"_id": app.state.settings.default_restaurant_id, "name": "Restaurant"})
    return _render(request, "confirmation.html", order=order, page="confirmation",
                   restaurant={"_id": order["restaurant_id"], "name": order["restaurant_name"]})
