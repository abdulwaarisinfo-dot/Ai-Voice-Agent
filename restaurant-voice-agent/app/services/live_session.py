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
import collections
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
        self.terminating = False  # set by the service the moment shutdown begins: no tool may run after this
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
                if self.closed or self.terminating:  # shutdown in progress: no tool run, no paid response.create
                    log.info("ignored tool call during shutdown")
                    return []
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
        if self.closed or self.terminating:
            return {"ok": False, "error_code": "call_ended", "message": "The call has ended. Nothing was changed."}
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
    wake: asyncio.Event = field(default_factory=asyncio.Event)  # set when termination begins
    attach_failed: bool = False
    cancel_requested: bool = False
    end_requested: bool = False
    terminating: bool = False        # one-way latch: once True the call only closes, never works
    term_reason: str | None = None
    close_sent: bool = False         # session.close actually written to the provider socket
    greeted: bool = False
    started: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    status: str = "creating"  # creating | connecting | active | ended | failed | cancelled
    task: asyncio.Task | None = None


def _default_connect(url: str, headers: dict):
    import websockets
    return websockets.connect(url, additional_headers=headers, open_timeout=10, max_size=2**22)


class LiveService:
    """Lifecycle of every call. All state changes happen on the event loop (never from worker threads).

    Provider close: the ONLY supported way to end a GPT-Live session is the `session.close` event on the
    session's sideband socket (the same event the browser may send on its data channel). Dropping the WebRTC
    peer, aborting an HTTP request, or POST /api/call/end alone does not close it server-side. So every path
    below ends in `_begin_termination` -> watchdog writes `session.close` on the sideband; if the sideband is
    lost, `_force_close_provider` re-attaches just to send it.
    """
    READY_TIMEOUT_S = 45         # browser never confirmed `ready` -> close (the browser itself gives up at 20s)
    RESTAURANT_CHECK_S = 10      # fallback for restaurants archived by another worker process
    SESSION_CLOSED_WAIT_S = 15   # wait for session.closed (usage) after session.close
    CLOSE_RETRIES = 2            # re-attach attempts when the sideband is lost
    CLOSE_RETRY_DELAY_S = 0.3
    FORCE_CLOSE_WAIT_S = 5
    TOMBSTONE_TTL_S = 900
    TOMBSTONE_MAX = 2048

    def __init__(self, db, settings: Settings, client, connect=_default_connect):
        self.db, self.settings, self.client, self._connect = db, settings, client, connect
        self.calls: dict[str, CallState] = {}
        self._cancelled: "collections.OrderedDict[str, float]" = collections.OrderedDict()  # cancel-before-create
        self._bg: set[asyncio.Task] = set()

    # ----- helpers -----
    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    def _remember_cancel(self, call_id: str) -> None:
        now = time.monotonic()
        self._cancelled[call_id] = now
        while self._cancelled and (len(self._cancelled) > self.TOMBSTONE_MAX
                                   or now - next(iter(self._cancelled.values())) > self.TOMBSTONE_TTL_S):
            self._cancelled.popitem(last=False)

    def _was_cancelled(self, call_id: str) -> bool:
        ts = self._cancelled.get(call_id)
        return ts is not None and time.monotonic() - ts <= self.TOMBSTONE_TTL_S

    def _begin_termination(self, call: CallState, reason: str) -> bool:
        """Idempotent latch. Returns True only for the first caller. After this no tool runs and nothing
        queued (greeting, tool results, response.create) is ever sent: only `session.close`."""
        if call.terminating:
            return False
        call.terminating, call.term_reason = True, reason
        call.controller.terminating = True
        while not call.outbox.empty():
            call.outbox.get_nowait()
        call.wake.set()
        return True

    def _restaurant_active(self, restaurant_id: str) -> bool:
        return menu_service.get_restaurant(self.db, restaurant_id) is not None

    async def _db_update(self, call_id: str, changes: dict, only_if_status: str | None = None) -> None:
        query = {"_id": call_id} if only_if_status is None else {"_id": call_id, "status": only_if_status}
        try:
            await asyncio.to_thread(self.db.voice_sessions.update_one, query, {"$set": changes})
        except Exception as exc:  # a database hiccup must never keep a paid session open
            log.error("voice_sessions update failed: %s", type(exc).__name__)

    # ----- lifecycle -----
    def active_count(self) -> int:
        return len(self.calls)

    async def create_call(self, restaurant: dict, call_id: str, sdp: str) -> tuple[str, str]:
        """Returns (call_secret, sdp_answer). Raises CallError."""
        if not self.settings.openai_api_key:
            raise CallError(503, "Voice service is not configured.")
        if self._was_cancelled(call_id):  # the browser's cancel overtook this request
            raise CallError(409, "Call was cancelled.")
        if call_id in self.calls:
            raise CallError(409, "Call already exists.")
        if self.active_count() >= self.settings.max_concurrent_calls:
            raise CallError(503, "All lines are busy. Please try again shortly.")
        secret = secrets.token_urlsafe(24)
        call = CallState(call_id, restaurant, CallController(self.db, restaurant["_id"], call_id))
        self.calls[call_id] = call  # reserves the concurrency slot; released only by _finish
        try:
            await asyncio.to_thread(self.db.voice_sessions.insert_one, {
                "_id": call_id, "restaurant_id": restaurant["_id"], "call_secret_hash": hash_token(secret),
                "status": "creating", "created_at": datetime.now(timezone.utc)})
        except DuplicateKeyError:
            self.calls.pop(call_id, None)
            raise CallError(409, "Call already exists.")
        except BaseException as exc:
            self.calls.pop(call_id, None)  # never leak the slot, even if this request is cancelled
            if isinstance(exc, asyncio.CancelledError):
                raise
            log.error("voice_sessions insert failed: %s", type(exc).__name__)
            raise CallError(503, "The voice service is unavailable right now.")

        # The provider call runs as its own task: if this HTTP request is cancelled (browser aborted, tab closed)
        # the creation still completes, and _adopt_late_session closes whatever it created.
        create_task = asyncio.ensure_future(self.client.live.create(
            session=build_session_config(self.settings, restaurant), transport={"type": "webrtc", "sdp": sdp}))
        try:
            result = await asyncio.shield(create_task)
            call.session_id, answer = result.session.id, result.transport.sdp
        except asyncio.CancelledError:
            self._begin_termination(call, "client_disconnected")
            create_task.add_done_callback(lambda fut: self._spawn(self._adopt_late_session(call, fut)))
            raise
        except Exception as exc:  # log the class/status only: never the request, key or SDP
            log.error("live session creation failed: %s status=%s", type(exc).__name__, getattr(exc, "status_code", None))
            await self._finish(call, "failed", "create_failed")
            raise CallError(502, "The voice service is unavailable right now.")

        # The sideband task is the ONLY thing that can close this provider session, so it is started before any
        # await that a cancelled request could interrupt (otherwise the session would be orphaned).
        call.task = self._spawn(self._run_sideband(call))
        try:
            if not call.terminating:
                call.status = "connecting"
            await self._db_update(call_id, {"openai_session_id": call.session_id})
            await self._db_update(call_id, {"status": "connecting"}, only_if_status="creating")
            if call.terminating and not call.attach_failed:  # cancelled / archived while OpenAI was creating the session
                raise CallError(409, "Call was cancelled.")  # the sideband task will close the new session
            try:
                await asyncio.wait_for(call.attached.wait(), 12)
            except asyncio.TimeoutError:
                call.attach_failed = True
            if call.attach_failed:
                self._begin_termination(call, "attach_failed")
                raise CallError(503, "The ordering system could not be reached. Please try again.")
            if call.terminating:
                raise CallError(409, "Call was cancelled.")
        except asyncio.CancelledError:
            self._begin_termination(call, "client_disconnected")  # sideband task (already spawned) closes it
            raise
        return secret, answer

    async def _adopt_late_session(self, call: CallState, fut: "asyncio.Future") -> None:
        """The request that asked for this session is gone. Close whatever OpenAI created for it."""
        if fut.cancelled() or fut.exception() is not None:
            await self._finish(call, "failed" if not fut.cancelled() else "cancelled", "create_failed" if not fut.cancelled() else "cancelled_by_user")
            return
        call.session_id = fut.result().session.id
        await self._db_update(call.call_id, {"openai_session_id": call.session_id})
        await self._run_sideband(call)  # terminating is already latched: it attaches only to send session.close

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
        self._remember_cancel(call_id)  # also covers a cancel that arrives BEFORE its /session request
        call = self.calls.get(call_id)
        if call is None:
            return False
        call.cancel_requested = True
        self._begin_termination(call, "cancelled")
        return True

    def request_end(self, call_id: str) -> bool:
        call = self.calls.get(call_id)
        if call is None:
            return False
        call.end_requested = True
        self._begin_termination(call, "ended_by_user")
        return True

    def terminate_restaurant(self, restaurant_id: str, reason: str = "restaurant_unavailable") -> int:
        """Close every live call of a restaurant that was archived. Returns how many were told to close."""
        n = 0
        for call in list(self.calls.values()):
            if call.restaurant["_id"] == restaurant_id and self._begin_termination(call, reason):
                n += 1
        return n

    async def mark_ready(self, call_id: str) -> None:
        """Browser saw session.started. Only now do we call the session active and send the greeting."""
        call = self.calls.get(call_id)
        if call is None or call.greeted or call.terminating or call.status in ("ended", "failed", "cancelled"):
            return
        call.greeted, call.status = True, "active"
        await self._db_update(call_id, {"status": "active"})
        if call.terminating:  # latched while the DB write was in flight: no greeting (it would be paid audio)
            return
        call.outbox.put_nowait({
            "type": "session.commentary.append", "event_id": "greeting_1", "delegation_id": None,
            "content": f"Greet the customer warmly as the voice ordering assistant for {call.restaurant['name']} and ask how you can help."})

    # ----- sideband -----
    async def _run_sideband(self, call: CallState) -> None:
        url = SIDEBAND_URL.format(session_id=call.session_id)
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        final = ("ended", None)
        shutdown = False
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
                            if not call.terminating:  # a tool that finished while we shut down must not answer
                                call.outbox.put_nowait(out)
                        if call.controller.closed:
                            break
                finally:
                    writer.cancel()
                    watchdog.cancel()
                    await asyncio.gather(writer, watchdog, return_exceptions=True)  # no task outlives the call
        except asyncio.CancelledError:  # server shutdown / loop teardown: still close the provider session
            shutdown = True
            final = ("ended", "server_shutdown")
        except Exception as exc:
            log.error("sideband failed: %s", type(exc).__name__)
            call.attach_failed = not call.attached.is_set()
            final = ("failed", "sideband_error")
        finally:
            call.attached.set()
            self._begin_termination(call, "sideband_lost")  # nothing may run on a call whose socket is gone
            if not call.controller.closed and not call.close_sent:
                # socket vanished before we could say session.close; on shutdown do not wait for the reply
                await self._force_close_provider(call, wait=0 if shutdown else self.FORCE_CLOSE_WAIT_S)
            if final[0] == "ended":
                if call.cancel_requested:
                    final = ("cancelled", "cancelled_by_user")
                else:
                    final = ("ended", call.term_reason if call.term_reason not in (None, "sideband_lost") else
                             ("session_closed" if call.controller.closed else "sideband_lost"))
            await self._finish(call, *final)

    async def _force_close_provider(self, call: CallState, wait: float | None = None) -> None:
        """The sideband is gone but the provider session may still be open (and billing). Re-attach and send the
        supported `session.close`. Bounded: CLOSE_RETRIES attempts, each limited by FORCE_CLOSE_WAIT_S."""
        if not call.session_id:
            return
        url = SIDEBAND_URL.format(session_id=call.session_id)
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        for attempt in range(self.CLOSE_RETRIES):
            try:
                async with self._connect(url, headers) as ws:
                    await ws.send(json.dumps({"type": "session.close", "event_id": "close_forced"}))
                    call.close_sent = True
                    if wait:
                        try:
                            await asyncio.wait_for(self._await_closed(call, ws), wait)
                        except asyncio.TimeoutError:
                            pass
                    return
            except Exception as exc:
                log.error("forced close attempt %d failed: %s", attempt + 1, type(exc).__name__)
                await asyncio.sleep(self.CLOSE_RETRY_DELAY_S)
        log.error("could not close provider session; it will end by the provider's own timeout")

    async def _await_closed(self, call: CallState, ws) -> None:
        async for raw in ws:
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if event.get("type") == "session.closed":
                call.controller.closed, call.controller.usage = True, event.get("usage")
                return

    async def _writer(self, call: CallState, ws) -> None:
        while True:
            msg = await call.outbox.get()
            if call.terminating and msg.get("type") != "session.close":
                continue  # defence in depth: only the close goes out once shutdown began
            await ws.send(json.dumps(msg))
            if msg.get("type") == "session.close":
                call.close_sent = True

    async def _watchdog(self, call: CallState, ws) -> None:
        try:
            await self._watchdog_loop(call, ws)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never stop guarding silently: an unexpected failure closes the call
            log.error("watchdog failed: %s", type(exc).__name__)
            self._begin_termination(call, "watchdog_error")
            call.outbox.put_nowait({"type": "session.close", "event_id": "close_watchdog_error"})

    async def _watchdog_loop(self, call: CallState, ws) -> None:
        """Cost + safety: closes the call on cancel/end (immediately, via call.wake), when the browser vanishes,
        when the restaurant is archived, or at the time cap. Exactly one session.close is queued."""
        s = self.settings
        next_check = time.monotonic() + self.RESTAURANT_CHECK_S
        while not call.terminating:
            try:
                await asyncio.wait_for(call.wake.wait(), 0.5)
            except asyncio.TimeoutError:
                pass
            if call.terminating:
                break
            now = time.monotonic()
            if now - call.started > s.max_call_seconds:
                self._begin_termination(call, "max_duration")
            elif call.greeted and now - call.last_seen > s.idle_timeout_seconds:
                self._begin_termination(call, "browser_gone")
            elif not call.greeted and now - call.started > self.READY_TIMEOUT_S:
                self._begin_termination(call, "never_ready")
            elif now >= next_check:
                next_check = now + self.RESTAURANT_CHECK_S
                try:
                    active = await asyncio.to_thread(self._restaurant_active, call.restaurant["_id"])
                except Exception as exc:  # unknown != archived: keep the call, keep the time caps running
                    log.error("restaurant check failed: %s", type(exc).__name__)
                    active = True
                if not active:
                    self._begin_termination(call, "restaurant_unavailable")
        call.outbox.put_nowait({"type": "session.close", "event_id": f"close_{call.term_reason}"})
        for _ in range(int(self.SESSION_CLOSED_WAIT_S / 0.5)):  # wait for session.closed (final usage)
            if call.controller.closed:
                return
            await asyncio.sleep(0.5)
        await ws.close()

    async def _finish(self, call: CallState, status: str, reason: str) -> None:
        """Idempotent: the first caller wins, later signals (session.closed racing a timeout, ...) are no-ops."""
        if call.status in ("ended", "failed", "cancelled"):
            return
        call.status = status
        self.calls.pop(call.call_id, None)  # releases the concurrency slot
        self._begin_termination(call, reason)
        await self._db_update(call.call_id, {
            "status": status, "end_reason": reason, "ended_at": datetime.now(timezone.utc), "usage": call.controller.usage})
