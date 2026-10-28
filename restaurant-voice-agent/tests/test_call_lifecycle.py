"""Termination + cost-safety paths. Fake OpenAI client, fake sideband socket, controlled async events:
nothing here touches the network or spends money."""
import asyncio
import json

import pytest

from app.services.live_session import CallError, LiveService
from tests.conftest import FakeConnect, FakeLive, FakeWS, SDP, call_id, make_settings, tool_event


class CountingConnect(FakeConnect):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.attempts = 0

    def __call__(self, url, headers):
        self.attempts += 1
        return super().__call__(url, headers)


async def until(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def service(db, conn=None, live=None, **settings):
    svc = LiveService(db, make_settings(**settings), live or FakeLive(), connect=conn or FakeConnect())
    svc.CLOSE_RETRY_DELAY_S, svc.FORCE_CLOSE_WAIT_S = 0.02, 0.2
    return svc


def resto(db, rid="resto-a"):
    return db.restaurants.find_one({"_id": rid})


def row(db, n=1):
    return db.voice_sessions.find_one({"_id": call_id(n)})


async def start(svc, db, n=1, ready=True):
    secret, _ = await svc.create_call(resto(db), call_id(n), SDP)
    if ready:
        await svc.mark_ready(call_id(n))
    return secret


def closes(conn):
    return [m for m in conn.ws.sent if m["type"] == "session.close"]


async def test_cancel_that_overtakes_session_creation_never_reaches_openai(db):
    live = FakeLive()
    svc = service(db, live=live)
    assert svc.request_cancel(call_id()) is False          # unknown yet: remembered, not an error
    with pytest.raises(CallError) as e:
        await svc.create_call(resto(db), call_id(), SDP)
    assert e.value.status == 409 and live.calls == [] and svc.active_count() == 0


async def test_request_cancelled_mid_creation_closes_the_late_session(db):
    gate, conn = asyncio.Event(), CountingConnect()
    live = FakeLive(gate=gate)
    svc = service(db, conn=conn, live=live)
    task = asyncio.create_task(svc.create_call(resto(db), call_id(), SDP))
    await asyncio.sleep(0.05)
    task.cancel()                                           # browser aborted / tab closed -> handler cancelled
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()                                              # OpenAI answers AFTER the request is gone
    assert await until(lambda: closes(conn)), "late session was left open"
    conn.ws.incoming.put({"type": "session.closed", "usage": {"seconds": 0}})
    assert await until(lambda: svc.active_count() == 0 and row(db)["status"] in ("ended", "cancelled"))
    assert row(db)["openai_session_id"] == "live_test_1"
    assert len(live.calls) == 1


async def test_late_creation_failure_after_cancel_releases_everything(db):
    gate, conn = asyncio.Event(), CountingConnect()
    svc = service(db, conn=conn, live=FakeLive(fail=True, gate=gate))
    task = asyncio.create_task(svc.create_call(resto(db), call_id(), SDP))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    assert await until(lambda: svc.active_count() == 0 and row(db)["status"] == "failed")
    assert conn.attempts == 0


async def test_end_requested_repeatedly_sends_one_close_and_finishes_once(db):
    conn = FakeConnect()
    svc = service(db, conn=conn)
    await start(svc, db)
    for _ in range(5):
        svc.request_end(call_id())
    svc.request_cancel(call_id())
    assert await until(lambda: closes(conn))
    await asyncio.sleep(0.8)
    assert len(closes(conn)) == 1
    conn.ws.incoming.put({"type": "session.closed", "usage": {"seconds": 9}})
    conn.ws.incoming.put({"type": "session.closed", "usage": {"seconds": 99}})   # duplicate closed event
    assert await until(lambda: row(db)["status"] in ("ended", "cancelled"))
    assert row(db)["usage"] == {"seconds": 9} and svc.active_count() == 0


async def test_tool_call_arriving_during_shutdown_is_not_executed_or_answered(db):
    conn = FakeConnect()
    svc = service(db, conn=conn)
    await start(svc, db)
    svc.request_end(call_id())
    conn.ws.incoming.put(tool_event("late1", "set_order_items", {"items": [{"item_id": "a-naan", "quantity": 1}]}))
    assert await until(lambda: closes(conn))
    await asyncio.sleep(0.3)
    types = conn.ws.types()
    assert "response.create" not in types and "response.item.create" not in types
    assert db.orders.count_documents({}) == 0               # nothing executed, so no paid continuation either
    conn.ws.incoming.put({"type": "session.closed"})
    assert await until(lambda: svc.active_count() == 0)


async def test_queued_greeting_and_results_are_dropped_once_terminating(db):
    conn = FakeConnect()
    svc = service(db, conn=conn)
    await start(svc, db, ready=False)
    call = svc.calls[call_id()]
    call.outbox.put_nowait({"type": "response.create", "event_id": "stale"})
    svc.request_end(call_id())
    await svc.mark_ready(call_id())                         # ready arriving after end must not greet
    assert await until(lambda: closes(conn))
    assert [t for t in conn.ws.types() if t != "session.close"] == []


async def test_sideband_drop_triggers_forced_provider_close(db):
    conn, live = CountingConnect(), FakeLive()
    svc = service(db, conn=conn, live=live)
    await start(svc, db)
    await conn.ws.close()                                   # socket vanishes, no session.closed
    assert await until(lambda: svc.active_count() == 0 and row(db)["status"] == "ended")
    assert conn.attempts >= 2                               # re-attached just to say session.close
    assert any(m.get("event_id") == "close_forced" for m in conn.ws.sent)
    assert len(live.calls) == 1 and "session.start" not in conn.ws.types()   # attach only: never a second session
    assert row(db)["end_reason"] == "sideband_lost"


async def test_sideband_that_never_attaches_is_retried_for_close_then_released(db):
    conn = CountingConnect(fail=True)
    svc = service(db, conn=conn)
    with pytest.raises(CallError) as e:
        await svc.create_call(resto(db), call_id(), SDP)
    assert e.value.status == 503
    assert await until(lambda: svc.active_count() == 0)
    assert conn.attempts == 1 + svc.CLOSE_RETRIES            # bounded, no retry storm
    assert await until(lambda: row(db)["status"] == "failed")


async def test_close_already_sent_is_not_repeated_by_forced_path(db):
    conn = CountingConnect()
    svc = service(db, conn=conn)
    svc.SESSION_CLOSED_WAIT_S = 1
    await start(svc, db)
    svc.request_end(call_id())
    assert await until(lambda: svc.active_count() == 0, 4)   # no session.closed: watchdog closes the socket
    assert len(closes(conn)) == 1 and conn.attempts == 1


async def test_time_cap_idle_and_never_ready_each_close_the_session(db):
    for n, kw, setup in ((1, dict(max_call_seconds=1), True), (2, dict(idle_timeout_seconds=1), True), (3, {}, False)):
        conn = FakeConnect()
        svc = service(db, conn=conn, **kw)
        svc.READY_TIMEOUT_S = 1
        await start(svc, db, n=n, ready=setup)
        assert await until(lambda: closes(conn), 4), f"case {n}"
        conn.ws.incoming.put({"type": "session.closed"})
        assert await until(lambda: svc.active_count() == 0)
        assert row(db, n)["end_reason"] in ("max_duration", "browser_gone", "never_ready")


async def test_old_call_timers_cannot_touch_a_newer_call(db):
    c1, c2 = FakeConnect(), FakeConnect()
    s1 = service(db, conn=c1)
    s2 = LiveService(db, make_settings(), FakeLive(), connect=c2)
    s2.CLOSE_RETRY_DELAY_S = 0.02
    await start(s1, db, n=1)
    s1.request_end(call_id(1))
    c1.ws.incoming.put({"type": "session.closed"})
    assert await until(lambda: s1.active_count() == 0)
    await start(s2, db, n=2)
    await asyncio.sleep(1.2)                                 # long enough for any stale 0.5s timer to fire
    assert closes(c2) == [] and s2.active_count() == 1
    s2.request_end(call_id(2))
    c2.ws.incoming.put({"type": "session.closed"})


async def test_finished_call_leaves_no_tasks_and_status_stays_readable(db):
    conn = FakeConnect()
    svc = service(db, conn=conn)
    await start(svc, db)
    call = svc.calls[call_id()]
    svc.request_end(call_id())
    conn.ws.incoming.put({"type": "session.closed", "usage": {"seconds": 3}})
    assert await until(lambda: svc.active_count() == 0)
    assert await until(lambda: call.task.done() and not svc._bg)
    assert svc.live_status(row(db)) == "ended"
    assert svc.request_end(call_id()) is False               # repeated end after finish is a harmless no-op


async def test_cancel_during_post_create_db_write_still_closes_the_session(db):
    """Regression: the request was cancelled after OpenAI created the session but before the sideband started."""
    conn, gate = CountingConnect(), asyncio.Event()
    svc = service(db, conn=conn)
    real = svc._db_update

    async def slow(call_id_, changes, only_if_status=None):
        if "openai_session_id" in changes:
            await gate.wait()
        await real(call_id_, changes, only_if_status)

    svc._db_update = slow
    task = asyncio.create_task(svc.create_call(resto(db), call_id(), SDP))
    assert await until(lambda: call_id() in svc.calls and svc.calls[call_id()].session_id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await until(lambda: closes(conn)), "orphaned provider session"
    conn.ws.incoming.put({"type": "session.closed"})
    assert await until(lambda: svc.active_count() == 0)       # slot released
    gate.set()


async def test_database_failure_in_restaurant_check_does_not_disable_the_time_cap(db):
    """Regression: an exception in the periodic restaurant check used to kill the watchdog silently."""
    conn = FakeConnect()
    svc = service(db, conn=conn, max_call_seconds=1)
    svc.RESTAURANT_CHECK_S = 0.05

    def boom(_rid):
        raise RuntimeError("mongo down")

    svc._restaurant_active = boom
    await start(svc, db)
    assert await until(lambda: closes(conn), 4)
    conn.ws.incoming.put({"type": "session.closed"})
    assert await until(lambda: svc.active_count() == 0 and row(db)["status"] == "ended")
    assert row(db)["end_reason"] == "max_duration"


async def test_unexpected_watchdog_error_fails_safe_by_closing(db):
    conn = FakeConnect()
    svc = service(db, conn=conn)

    async def broken(call, ws):
        raise RuntimeError("bug")

    svc._watchdog_loop = broken
    with pytest.raises(CallError):                           # the call is closed before it was ever handed to the browser
        await start(svc, db)
    assert await until(lambda: closes(conn))
    conn.ws.incoming.put({"type": "session.closed"})
    assert await until(lambda: svc.active_count() == 0)
