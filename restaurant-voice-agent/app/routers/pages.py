"""HTML pages. Opening any of them never contacts OpenAI or creates a session."""
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..services import order_service
from .deps import resolve_restaurant

router = APIRouter()
log = logging.getLogger("pages")


def _page_restaurant(request: Request, restaurant_id: str | None) -> dict | None:
    """Restaurant for an HTML page, or None when the *implicit default* restaurant has no record yet.

    Only that one case is softened into a setup page. An explicitly requested id that does not exist is
    still a 404, and database errors (timeouts, auth failures, ...) are not caught here: they stay 500s
    instead of being disguised as "restaurant not configured".
    """
    try:
        return resolve_restaurant(request, restaurant_id)
    except HTTPException as exc:
        if exc.status_code != 404 or restaurant_id:
            raise
        state = request.app.state
        log.warning("Default restaurant %r not found in database %r; rendering the setup page",
                    state.settings.default_restaurant_id, getattr(state.db, "name", "?"))
        return None


def _setup_page(request: Request, page: str):
    response = _render(request, "restaurant_setup.html", page=page)
    response.headers["Cache-Control"] = "no-store"  # must not outlive the fix
    return response


def _render(request: Request, name: str, status: int = 200, **ctx):
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


@router.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/call")


@router.get("/call", response_class=HTMLResponse)
def call_page(request: Request, restaurant_id: str | None = Query(None, max_length=64)):
    restaurant = _page_restaurant(request, restaurant_id)
    if restaurant is None:
        return _setup_page(request, "call")
    return _render(request, "call.html", restaurant=restaurant, page="call")


@router.get("/menu", response_class=HTMLResponse)
def menu_page(request: Request, restaurant_id: str | None = Query(None, max_length=64)):
    restaurant = _page_restaurant(request, restaurant_id)
    if restaurant is None:
        return _setup_page(request, "menu")
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
