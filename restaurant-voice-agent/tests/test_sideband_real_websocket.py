"""Exercises the real `websockets` client (auth header, JSON events, tool round-trip) against a local
server standing in for wss://api.openai.com/v1/live/sessions/{id}/attach. Not OpenAI itself."""
import asyncio
import json

import websockets
from websockets.asyncio.server import serve

from app.services import live_session
from app.services.live_session import LiveService
from tests.conftest import FakeLive, SDP, call_id, make_settings, tool_event


async def test_sideband_runs_tools_over_a_real_websocket(db, monkeypatch):
    seen = {}

    async def handler(ws):
        seen["auth"] = ws.request.headers.get("Authorization")
        seen["path"] = ws.request.path
        await ws.send(json.dumps(tool_event("w1", "set_order_items", {"items": [{"item_id": "a-naan", "quantity": 3}]})))
        replies = [json.loads(await asyncio.wait_for(ws.recv(), 3)) for _ in range(2)]
        seen["replies"] = replies
        await ws.send(json.dumps({"type": "session.closed", "usage": {"s": 1}}))
        await asyncio.sleep(0.2)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(live_session, "SIDEBAND_URL", f"ws://127.0.0.1:{port}/v1/live/sessions/{{session_id}}/attach")
        svc = LiveService(db, make_settings(), FakeLive())
        secret, answer = await svc.create_call(db.restaurants.find_one({"_id": "resto-a"}), call_id(), SDP)
        assert answer == "v=0 ANSWER" and secret
        for _ in range(150):
            if db.voice_sessions.find_one({"_id": call_id()})["status"] == "ended":
                break
            await asyncio.sleep(0.02)
    assert seen["auth"].startswith("Bearer sk-test") and seen["path"] == "/v1/live/sessions/live_test_1/attach"
    assert [r["type"] for r in seen["replies"]] == ["response.item.create", "response.create"]
    out = json.loads(seen["replies"][0]["item"]["output"])
    assert out["ok"] and out["order"]["total"] == "PKR 360.00"
    assert db.voice_sessions.find_one({"_id": call_id()})["usage"] == {"s": 1}
