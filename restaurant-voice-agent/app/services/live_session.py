"""GPT-Live integration (verified against OpenAI docs, Oct 2026).

Flow
  browser --SDP offer--> POST /api/call/session --> POST https://api.openai.com/v1/live/sessions (API key, server only)
  OpenAI returns {session:{id}, transport:{sdp}}; we return ONLY the SDP answer + a call secret.
  Server then attaches a *sideband* WebSocket  wss://api.openai.com/v1/live/sessions/{id}/attach
  GPT-Live delegates to a Responses model; its function calls arrive on the sideband, we run them
  against MongoDB bound to the restaurant of THIS call, and send the results back.
Audio never touches this server.
"""
import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from ..config import Settings
from ..models.schemas import CustomerIn, OrderItemsIn
from ..security import hash_token
from . import menu_service, order_service
from .order_service import OrderError, validation_message
from .prompts import backend_instructions, live_instructions

log = logging.getLogger("live")
SIDEBAND_URL = "wss://api.openai.com/v1/live/sessions/{session_id}/attach"

TOOLS = [
    {"type": "function", "name": "get_menu",
     "description": "Get this restaurant's menu with verified prices, availability and customization options. Call before answering menu questions.",
     "parameters": {"type": "object", "properties": {"category": {"type": "string", "description": "Optional category filter"}}, "additionalProperties": False}},
    {"type": "function", "name": "set_order_items",
     "description": "Replace the order's items. Server prices everything from the database.",
     "parameters": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "item_id": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1, "maximum": 20},
         "customizations": {"type": "array", "items": {"type": "string"}}},
         "required": ["item_id", "quantity"], "additionalProperties": False}}}, "required": ["items"], "additionalProperties": False}},
    {"type": "function", "name": "set_customer_details",
     "description": "Save customer name, phone, and pickup/delivery choice (address required for delivery).",
     "parameters": {"type": "object", "properties": {"customer_name": {"type": "string"}, "phone": {"type": "string"},
         "fulfillment": {"type": "string", "enum": ["pickup", "delivery"]}, "delivery_address": {"type": "string"}},
         "required": ["customer_name", "phone", "fulfillment"], "additionalProperties": False}},
    {"type": "function", "name": "get_order_summary", "description": "Current order with server-calculated totals.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "request_confirmation",
     "description": "Re-verify the order and get the read-back text and quote_hash. Does NOT place the order.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"type": "function", "name": "confirm_order",
     "description": "Place the order. Only after the customer said yes to the read-back.",
     "parameters": {"type": "object", "properties": {"quote_hash": {"type": "string"}, "customer_confirmed": {"type": "boolean"}},
                    "required": ["quote_hash", "customer_confirmed"], "additionalProperties": False}},
]


def build_session_config(settings: Settings, restaurant: dict) -> dict:
    """Server-side only: the browser cannot influence model, instructions or tools."""
    return {
        "model": settings.openai_live_model,
        "instructions": live_instructions(restaurant["name"]),
        "delegation": {"type": "responses", "responses": {
            "model": settings.openai_delegation_model,
            "instructions": backend_instructions(restaurant["name"]),
            "tools": TOOLS, "tool_choice": "auto", "parallel_tool_calls": False}},
    }


class CallError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class CallController:
    """Executes tool calls for ONE call. restaurant_id and call_id are fixed at construction,
    so neither the model nor the browser can address another restaurant or order."""

    def __init__(self, db, restaurant_id: str, call_id: str):
        self.db, self.restaurant_id, self.call_id = db, restaurant_id, call_id
        self.closed = False
        self.usage = None
        self.awaiting_customer_reply = False  # read-back happened, customer has not spoken since
        self.customer_replied = False
        self._results: dict[str, str] = {}  # call_id -> output, so a repeated tool call never re-executes

    # ----- events from OpenAI -----
    def handle_event(self, event: dict) -> list[dict]:
        etype = event.get("type")
        if etype == "session.input_transcript.delta":
            if self.awaiting_customer_reply and str(event.get("delta", "")).strip():
                self.customer_replied = True
        elif etype == "response.event":
            inner = event.get("event") or {}
            item = inner.get("item") or {}
            if inner.get("type") == "response.output_item.done" and item.get("type") == "function_call":
                return self._run_function_call(item)
        elif etype == "session.closed":
            self.closed = True
            self.usage = event.get("usage")
        elif etype == "error":
            err = event.get("error") or {}
            log.warning("live error code=%s", err.get("code") or event.get("code"))
        return []

    def _run_function_call(self, item: dict) -> list[dict]:
        cid = item.get("call_id")
        if not cid:
            return []
        if cid not in self._results:
            self._results[cid] = json.dumps(self.execute_tool(item.get("name", ""), item.get("arguments") or "{}"))
        return [
            {"type": "response.item.create", "event_id": f"result_{cid}",
             "item": {"type": "function_call_output", "call_id": cid, "output": self._results[cid]}},
            {"type": "response.create", "event_id": f"continue_{cid}"},
        ]

    # ----- tools -----
    def execute_tool(self, name: str, raw_args: str) -> dict:
        try:
            args = json.loads(raw_args) if raw_args else {}
            if not isinstance(args, dict):
                raise OrderError("invalid_input", "Arguments must be a JSON object.")
            return {"ok": True, **self._dispatch(name, args)}
        except OrderError as exc:
            return {"ok": False, "error_code": exc.code, "message": exc.message}
        except (ValidationError, json.JSONDecodeError) as exc:
            msg = validation_message(exc) if isinstance(exc, ValidationError) else "Arguments were not valid JSON."
            return {"ok": False, "error_code": "invalid_input", "message": msg}
        except Exception:  # never leak internals to the model; never claim success
            log.exception("tool %s failed", name)
            return {"ok": False, "error_code": "internal_error",
                    "message": "Something went wrong. The order was not changed. Do not tell the customer it succeeded."}

    def _dispatch(self, name: str, args: dict) -> dict:
        restaurant = menu_service.get_restaurant(self.db, self.restaurant_id)
        if restaurant is None:
            raise OrderError("unknown_restaurant", "Restaurant not found.")
        if name == "get_menu":
            items = menu_service.list_menu(self.db, restaurant, args.get("category"))
            return {"restaurant": restaurant["name"], "currency": restaurant["currency"],
                    "delivery_fee": order_service.format_money(restaurant.get("delivery_fee_minor", 0), restaurant["currency"]),
                    "items": [{k: i[k] for k in ("id", "name", "description", "category", "price", "available", "options")} for i in items]}
        if name == "set_order_items":
            parsed = OrderItemsIn.model_validate(args)
            order_service.set_items(self.db, self.restaurant_id, self.call_id, parsed.items)
            self._invalidate_readback()
            return {"order": order_service.summary(self.db, self.restaurant_id, self.call_id)}
        if name == "set_customer_details":
            order_service.set_customer(self.db, self.restaurant_id, self.call_id, CustomerIn.model_validate(args))
            self._invalidate_readback()
            return {"order": order_service.summary(self.db, self.restaurant_id, self.call_id)}
        if name == "get_order_summary":
            return {"order": order_service.summary(self.db, self.restaurant_id, self.call_id)}
        if name == "request_confirmation":
            result = order_service.request_confirmation(self.db, self.restaurant_id, self.call_id)
            self.awaiting_customer_reply, self.customer_replied = True, False
            return result
        if name == "confirm_order":
            if args.get("customer_confirmed") is not True:
                raise OrderError("not_confirmed", "The customer has not confirmed. Do not place the order.")
            if self.awaiting_customer_reply and not self.customer_replied:
                raise OrderError("customer_has_not_replied", "The customer has not answered since the read-back. Wait for their reply.")
            quote = str(args.get("quote_hash", ""))
            return order_service.confirm_order(self.db, self.restaurant_id, self.call_id, quote)
        raise OrderError("unknown_tool", f"Unknown tool '{name}'.")

    def _invalidate_readback(self):
        self.awaiting_customer_reply = self.customer_replied = False


@dataclass
class CallState:
    call_id: str
    restaurant: dict
    controller: CallController
    session_id: str | None = None
    outbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    attached: asyncio.Event = field(default_factory=asyncio.Event)
    attach_failed: bool = False
    cancel_requested: bool = False
    end_requested: bool = False
    greeted: bool = False
    started: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    status: str = "creating"  # creating | connecting | active | ended | failed | cancelled
    task: asyncio.Task | None = None


def _default_connect(url: str, headers: dict):
    import websockets
    return websockets.connect(url, additional_headers=headers, open_timeout=10, max_size=2**22)


class LiveService:
    def __init__(self, db, settings: Settings, client, connect=_default_connect):
        self.db, self.settings, self.client, self._connect = db, settings, client, connect
        self.calls: dict[str, CallState] = {}

    # ----- lifecycle -----
    def active_count(self) -> int:
        return len(self.calls)

    async def create_call(self, restaurant: dict, call_id: str, sdp: str) -> tuple[str, str]:
        """Returns (call_secret, sdp_answer). Raises CallError."""
        if not self.settings.openai_api_key:
            raise CallError(503, "Voice service is not configured.")
        if call_id in self.calls:
            raise CallError(409, "Call already exists.")
        if self.active_count() >= self.settings.max_concurrent_calls:
            raise CallError(503, "All lines are busy. Please try again shortly.")
        secret = secrets.token_urlsafe(24)
        call = CallState(call_id, restaurant, CallController(self.db, restaurant["_id"], call_id))
        self.calls[call_id] = call
        try:
            await asyncio.to_thread(self.db.voice_sessions.insert_one, {
                "_id": call_id, "restaurant_id": restaurant["_id"], "call_secret_hash": hash_token(secret),
                "status": "creating", "created_at": datetime.now(timezone.utc)})
        except DuplicateKeyError:
            self.calls.pop(call_id, None)
            raise CallError(409, "Call already exists.")
        try:
            result = await self.client.live.create(
                session=build_session_config(self.settings, restaurant), transport={"type": "webrtc", "sdp": sdp})
            call.session_id, answer = result.session.id, result.transport.sdp
        except Exception as exc:  # log the class/status only: never the request, key or SDP
            log.error("live session creation failed: %s status=%s", type(exc).__name__, getattr(exc, "status_code", None))
            await self._finish(call, "failed", "create_failed")
            raise CallError(502, "The voice service is unavailable right now.")
        call.status = "connecting"
        await asyncio.to_thread(self.db.voice_sessions.update_one, {"_id": call_id}, {"$set": {"openai_session_id": call.session_id, "status": "connecting"}})
        call.task = asyncio.create_task(self._run_sideband(call))
        try:
            await asyncio.wait_for(call.attached.wait(), 12)
        except asyncio.TimeoutError:
            call.attach_failed = True
        if call.attach_failed:
            call.cancel_requested = True
            raise CallError(503, "The ordering system could not be reached. Please try again.")
        if call.cancel_requested:  # Cancel arrived while OpenAI was still creating the session
            raise CallError(409, "Call was cancelled.")
        return secret, answer

    def authenticate(self, call_id: str, secret: str) -> dict | None:
        """Returns the voice_sessions doc if the call secret matches (constant-time), else None."""
        doc = self.db.voice_sessions.find_one({"_id": call_id})
        if doc is None or not secrets.compare_digest(doc["call_secret_hash"], hash_token(secret or "")):
            return None
        call = self.calls.get(call_id)
        if call:
            call.last_seen = time.monotonic()  # browser heartbeat
        return doc

    def live_status(self, doc: dict) -> str:
        call = self.calls.get(doc["_id"])
        return call.status if call else doc["status"]

    def request_cancel(self, call_id: str) -> bool:
        call = self.calls.get(call_id)
        if call is None:
            return False
        call.cancel_requested = True
        return True

    def request_end(self, call_id: str) -> bool:
        call = self.calls.get(call_id)
        if call is None:
            return False
        call.end_requested = True
        return True

    async def mark_ready(self, call_id: str) -> None:
        """Browser saw session.started. Only now do we call the session active and send the greeting."""
        call = self.calls.get(call_id)
        if call is None or call.greeted or call.status in ("ended", "failed", "cancelled"):
            return
        call.greeted, call.status = True, "active"
        await asyncio.to_thread(self.db.voice_sessions.update_one, {"_id": call_id}, {"$set": {"status": "active"}})
        call.outbox.put_nowait({
            "type": "session.commentary.append", "event_id": "greeting_1", "delegation_id": None,
            "content": f"Greet the customer warmly as the voice ordering assistant for {call.restaurant['name']} and ask how you can help."})

    # ----- sideband -----
    async def _run_sideband(self, call: CallState) -> None:
        url = SIDEBAND_URL.format(session_id=call.session_id)
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        final = ("ended", "session_closed")
        try:
            async with self._connect(url, headers) as ws:
                call.attached.set()
                writer = asyncio.create_task(self._writer(call, ws))
                watchdog = asyncio.create_task(self._watchdog(call, ws))
                try:
                    async for raw in ws:
                        try:
                            event = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        for out in await asyncio.to_thread(call.controller.handle_event, event):
                            call.outbox.put_nowait(out)
                        if call.controller.closed:
                            break
                finally:
                    writer.cancel()
                    watchdog.cancel()
        except Exception as exc:
            log.error("sideband failed: %s", type(exc).__name__)
            call.attach_failed = not call.attached.is_set()
            final = ("failed", "sideband_error")
        finally:
            call.attached.set()
            if call.cancel_requested and final[0] == "ended":
                final = ("cancelled", "cancelled_by_user")
            await self._finish(call, *final)

    async def _writer(self, call: CallState, ws) -> None:
        while True:
            await ws.send(json.dumps(await call.outbox.get()))

    async def _watchdog(self, call: CallState, ws) -> None:
        """Cost + safety: close the call on cancel/end, when the browser vanishes, or at the time cap."""
        s = self.settings
        reason = None
        while reason is None:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            if call.cancel_requested:
                reason = "cancelled"
            elif call.end_requested:
                reason = "ended_by_user"
            elif now - call.started > s.max_call_seconds:
                reason = "max_duration"
            elif call.greeted and now - call.last_seen > s.idle_timeout_seconds:
                reason = "browser_gone"
        call.outbox.put_nowait({"type": "session.close", "event_id": f"close_{reason}"})
        for _ in range(30):  # wait up to 15s for session.closed (lets in-flight work drain)
            if call.controller.closed:
                return
            await asyncio.sleep(0.5)
        await ws.close()

    async def _finish(self, call: CallState, status: str, reason: str) -> None:
        if call.status in ("ended", "failed", "cancelled"):
            return
        call.status = status
        self.calls.pop(call.call_id, None)
        await asyncio.to_thread(self.db.voice_sessions.update_one, {"_id": call.call_id}, {"$set": {
            "status": status, "end_reason": reason, "ended_at": datetime.now(timezone.utc), "usage": call.controller.usage}})
