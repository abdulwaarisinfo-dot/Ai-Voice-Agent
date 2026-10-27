"""Read-only order retrieval. There is deliberately NO HTTP endpoint that creates, edits or confirms an
order: those happen only through server-side tools bound to a live call (see live_session.py)."""
from fastapi import APIRouter, HTTPException, Query, Request

from ..services import order_service

router = APIRouter(prefix="/api")


@router.get("/orders/{reference}")
def get_order(request: Request, reference: str, t: str | None = Query(None, max_length=80)):
    app = request.app
    order = order_service.get_confirmed_order(app.state.db, app.state.secret, reference, t)
    if order is None:
        raise HTTPException(404, "Order not found")  # same answer for wrong ref, wrong token, or unconfirmed
    order["confirmed_at"] = order["confirmed_at"].isoformat() if order.get("confirmed_at") else None
    return order
