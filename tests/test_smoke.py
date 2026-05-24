"""End-to-end smoke test for phases 0-2.

Spins the server up in-process, runs through:
  - create project
  - mint extra invite
  - second member redeems invite
  - both connect via WS
  - send messages, verify fan-out
  - create a second channel
  - @mention while not focused -> mention frame delivered, unread state updates
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@asynccontextmanager
async def _server():
    tmpdir = tempfile.mkdtemp(prefix="cowork-test-")
    os.environ["COWORK_HOME"] = tmpdir

    # Import lazily so COWORK_HOME is picked up.
    from cowork.server.app import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    # Wait for startup.
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "server did not start"
    base = f"http://127.0.0.1:{port}"
    try:
        yield base
    finally:
        server.should_exit = True
        await task


def _ws_url(base: str, token: str, project_id: str) -> str:
    return base.replace("http://", "ws://") + f"/ws?token={token}&project_id={project_id}"


async def _recv_until(ws, predicate, timeout=2.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        remaining = end - asyncio.get_event_loop().time()
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        frame = json.loads(raw)
        if predicate(frame):
            return frame
    raise AssertionError("predicate not satisfied within timeout")


@pytest.mark.asyncio
async def test_phase_0_through_2_flow():
    async with _server() as base:
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as http:
            # Create project as Alice
            r = await http.post(
                "/projects",
                json={"name": "demo", "creator_display_name": "alice"},
            )
            assert r.status_code == 200, r.text
            alice = r.json()
            assert alice["default_invite_token"]

            # Bob redeems
            r = await http.post(
                "/invites/redeem",
                json={"invite_token": alice["default_invite_token"], "display_name": "bob"},
            )
            assert r.status_code == 200, r.text
            bob = r.json()

            # Bootstrap as Alice
            r = await http.get(
                f"/projects/{alice['project_id']}/bootstrap",
                headers={"Authorization": f"Bearer {alice['member_token']}"},
            )
            assert r.status_code == 200, r.text
            boot = r.json()
            assert len(boot["channels"]) == 1
            assert boot["channels"][0]["name"] == "general"
            assert {m["display_name"] for m in boot["members"]} == {"alice", "bob"}
            general_id = boot["channels"][0]["id"]

        # Connect both via WS
        async with websockets.connect(
            _ws_url(base, alice["member_token"], alice["project_id"])
        ) as ws_alice, websockets.connect(
            _ws_url(base, bob["member_token"], alice["project_id"])
        ) as ws_bob:
            # Each gets a hello
            await _recv_until(ws_alice, lambda f: f["type"] == "hello")
            await _recv_until(ws_bob, lambda f: f["type"] == "hello")

            # Bob marks himself focused on #general
            await ws_bob.send(
                json.dumps({"type": "mark_read", "data": {"channel_id": general_id}})
            )

            # Alice posts a normal message
            await ws_alice.send(
                json.dumps(
                    {
                        "type": "send_message",
                        "data": {"channel_id": general_id, "content": "hello bob"},
                    }
                )
            )
            # Both should see the message frame
            msg_a = await _recv_until(ws_alice, lambda f: f["type"] == "message")
            msg_b = await _recv_until(ws_bob, lambda f: f["type"] == "message")
            assert msg_a["data"]["message"]["content"] == "hello bob"
            assert msg_b["data"]["message"]["display_name"] == "alice"

            # Alice creates a new channel
            await ws_alice.send(
                json.dumps({"type": "create_channel", "data": {"name": "random"}})
            )
            ch_frame = await _recv_until(ws_bob, lambda f: f["type"] == "channel_created")
            random_id = ch_frame["data"]["channel"]["id"]
            assert ch_frame["data"]["channel"]["name"] == "random"

            # Alice mentions Bob in #random — Bob is focused on #general, so should get a mention frame
            await ws_alice.send(
                json.dumps(
                    {
                        "type": "send_message",
                        "data": {"channel_id": random_id, "content": "hey @bob, look here"},
                    }
                )
            )
            mention = await _recv_until(ws_bob, lambda f: f["type"] == "mention")
            assert mention["data"]["channel_id"] == random_id
            assert mention["data"]["by_display_name"] == "alice"

            # Bob's unread state should reflect 1 unread, 1 mention in #random
            await ws_bob.send(
                json.dumps({"type": "mark_read", "data": {"channel_id": general_id}})
            )
            # Force an unread refresh by re-marking. Then query bootstrap.
            async with httpx.AsyncClient(base_url=base, timeout=5.0) as http:
                r = await http.get(
                    f"/projects/{alice['project_id']}/bootstrap",
                    headers={"Authorization": f"Bearer {bob['member_token']}"},
                )
                state = r.json()
            random_unread = state["unread"][random_id]
            assert random_unread["count"] >= 1
            assert random_unread["mentions"] >= 1


@pytest.mark.asyncio
async def test_display_name_validation():
    async with _server() as base:
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as http:
            r = await http.post(
                "/projects",
                json={"name": "p", "creator_display_name": "bad name!"},
            )
            assert r.status_code == 400

            r = await http.post(
                "/projects",
                json={"name": "p", "creator_display_name": "here"},
            )
            assert r.status_code == 400


@pytest.mark.asyncio
async def test_duplicate_display_name_rejected():
    async with _server() as base:
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as http:
            r = await http.post(
                "/projects",
                json={"name": "p", "creator_display_name": "alice"},
            )
            alice = r.json()
            r = await http.post(
                "/invites/redeem",
                json={"invite_token": alice["default_invite_token"], "display_name": "alice"},
            )
            assert r.status_code == 400
