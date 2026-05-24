"""Agent dispatch tests (phases 3-4).

Uses MockAgentRuntime so no Anthropic API calls are made.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import websockets

from cowork.server.agent_runtime import MockAgentRuntime, set_runtime_override
from tests.conftest import (
    bootstrap,
    create_project,
    drain,
    recv_frame,
    redeem_invite,
    send,
    ws_url,
)


@pytest.fixture
def mock_runtime():
    runtime = MockAgentRuntime()
    set_runtime_override(runtime)
    yield runtime
    set_runtime_override(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_agent(
    client: httpx.AsyncClient,
    project_id: str,
    member_token: str,
    *,
    name: str,
    trigger: str = "on_mention",
    system_prompt: str = "",
    channel_id: str | None = None,
) -> dict:
    r = await client.post(
        f"/projects/{project_id}/agents",
        json={
            "display_name": name,
            "system_prompt": system_prompt,
            "trigger_mode": trigger,
            "model": None,
            "channel_id": channel_id,
        },
        headers={"Authorization": f"Bearer {member_token}"},
    )
    r.raise_for_status()
    return r.json()["agent"]


# ---------------------------------------------------------------------------
# Agent lifecycle
# ---------------------------------------------------------------------------


async def test_create_agent_adds_member_and_broadcasts(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
        await recv_frame(ws, "hello")
        agent = await _make_agent(
            client, pid, alice["member_token"], name="researcher", trigger="on_mention"
        )
        assert agent["display_name"] == "researcher"
        # Both events fan out to the creator's WS.
        member_joined = await recv_frame(ws, "member_joined")
        assert member_joined["data"]["member"]["display_name"] == "researcher"
        assert member_joined["data"]["member"]["is_agent"] is True
        agent_created = await recv_frame(ws, "agent_created")
        assert agent_created["data"]["agent"]["trigger_mode"] == "on_mention"


async def test_bootstrap_includes_agents(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    await _make_agent(client, pid, alice["member_token"], name="r1")
    await _make_agent(client, pid, alice["member_token"], name="r2", trigger="always")
    boot = await bootstrap(client, alice["member_token"], pid)
    names = {a["display_name"] for a in boot["agents"]}
    assert names == {"r1", "r2"}
    # is_agent flag reflected on members too.
    member_flags = {m["display_name"]: m["is_agent"] for m in boot["members"]}
    assert member_flags["alice"] is False
    assert member_flags["r1"] is True
    assert member_flags["r2"] is True


async def test_delete_agent_removes_member(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    agent = await _make_agent(client, pid, alice["member_token"], name="tmp")
    r = await client.delete(
        f"/projects/{pid}/agents/{agent['member_id']}",
        headers={"Authorization": f"Bearer {alice['member_token']}"},
    )
    assert r.status_code == 200
    boot = await bootstrap(client, alice["member_token"], pid)
    assert boot["agents"] == []
    names = {m["display_name"] for m in boot["members"]}
    assert "tmp" not in names


async def test_agent_creation_rejects_invalid_trigger(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    r = await client.post(
        f"/projects/{pid}/agents",
        json={"display_name": "x", "trigger_mode": "noisy"},
        headers={"Authorization": f"Bearer {alice['member_token']}"},
    )
    assert r.status_code in (400, 422)


async def test_agent_creation_rejects_reserved_name(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    r = await client.post(
        f"/projects/{pid}/agents",
        json={"display_name": "Channel", "trigger_mode": "on_mention"},
        headers={"Authorization": f"Bearer {alice['member_token']}"},
    )
    assert r.status_code == 400


async def test_agent_creation_rejects_duplicate_name(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    await _make_agent(client, pid, alice["member_token"], name="bot")
    r = await client.post(
        f"/projects/{pid}/agents",
        json={"display_name": "bot", "trigger_mode": "on_mention"},
        headers={"Authorization": f"Bearer {alice['member_token']}"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Trigger behavior
# ---------------------------------------------------------------------------


async def test_on_mention_agent_responds_to_at_name(
    server: str, client: httpx.AsyncClient, mock_runtime: MockAgentRuntime
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    await _make_agent(
        client, pid, alice["member_token"], name="helper", trigger="on_mention"
    )
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws, "register_api_key", api_key="sk-test")
        await recv_frame(ws, "api_key_registered")
        await send(ws, "send_message", channel_id=general_id, content="@helper hi")
        await recv_frame(ws, "message")  # alice's message echoes
        reply = await recv_frame(ws, "message")
        assert reply["data"]["message"]["display_name"] == "helper"
        assert "helper" in reply["data"]["message"]["content"]
    assert len(mock_runtime.calls) == 1
    assert "@helper" in mock_runtime.calls[0].user_prompt or "hi" in mock_runtime.calls[0].user_prompt


async def test_on_mention_agent_ignores_unrelated_messages(
    server: str, client: httpx.AsyncClient, mock_runtime: MockAgentRuntime
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    await _make_agent(
        client, pid, alice["member_token"], name="helper", trigger="on_mention"
    )
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws, "register_api_key", api_key="sk-test")
        await recv_frame(ws, "api_key_registered")
        await send(ws, "send_message", channel_id=general_id, content="just chatting")
        await recv_frame(ws, "message")
        # No further message frame from the agent.
        frames = await drain(ws, quiet_for=0.3)
        agent_replies = [
            f for f in frames if f.get("type") == "message"
            and f["data"]["message"]["display_name"] == "helper"
        ]
        assert agent_replies == []
    assert mock_runtime.calls == []


async def test_always_agent_responds_to_every_message(
    server: str, client: httpx.AsyncClient, mock_runtime: MockAgentRuntime
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    await _make_agent(
        client, pid, alice["member_token"], name="watcher", trigger="always"
    )
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws, "register_api_key", api_key="sk-test")
        await recv_frame(ws, "api_key_registered")
        await send(ws, "send_message", channel_id=general_id, content="weather report?")
        await recv_frame(ws, "message")
        reply = await recv_frame(ws, "message")
        assert reply["data"]["message"]["display_name"] == "watcher"


async def test_on_question_only_responds_to_questions(
    server: str, client: httpx.AsyncClient, mock_runtime: MockAgentRuntime
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    await _make_agent(
        client, pid, alice["member_token"], name="oracle", trigger="on_question"
    )
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws, "register_api_key", api_key="sk-test")
        await recv_frame(ws, "api_key_registered")
        # Statement: no reply.
        await send(ws, "send_message", channel_id=general_id, content="hello there")
        await recv_frame(ws, "message")
        frames = await drain(ws, quiet_for=0.3)
        assert not any(
            f.get("type") == "message" and f["data"]["message"]["display_name"] == "oracle"
            for f in frames
        )
        # Question: replies.
        await send(ws, "send_message", channel_id=general_id, content="what time is it?")
        await recv_frame(ws, "message")
        reply = await recv_frame(ws, "message")
        assert reply["data"]["message"]["display_name"] == "oracle"


# ---------------------------------------------------------------------------
# API key gating
# ---------------------------------------------------------------------------


async def test_agent_does_not_respond_without_api_key(
    server: str, client: httpx.AsyncClient, mock_runtime: MockAgentRuntime
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    await _make_agent(
        client, pid, alice["member_token"], name="helper", trigger="on_mention"
    )
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        # Note: no register_api_key call.
        await send(ws, "send_message", channel_id=general_id, content="@helper hi")
        await recv_frame(ws, "message")  # alice's message
        frames = await drain(ws, quiet_for=0.3)
        assert not any(
            f.get("type") == "message" and f["data"]["message"]["display_name"] == "helper"
            for f in frames
        )
    assert mock_runtime.calls == []


async def test_agent_goes_dormant_when_owner_disconnects(
    server: str, client: httpx.AsyncClient, mock_runtime: MockAgentRuntime
) -> None:
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    pid = alice["project_id"]
    await _make_agent(
        client, pid, alice["member_token"], name="helper", trigger="on_mention"
    )
    # Alice connects, registers key, disconnects.
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a:
        await recv_frame(ws_a, "hello")
        await send(ws_a, "register_api_key", api_key="sk-test")
        await recv_frame(ws_a, "api_key_registered")
    # Bob connects and mentions helper — but Alice (owner) is gone.
    async with websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b:
        hello = await recv_frame(ws_b, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws_b, "send_message", channel_id=general_id, content="@helper urgent")
        await recv_frame(ws_b, "message")
        frames = await drain(ws_b, quiet_for=0.3)
        assert not any(
            f.get("type") == "message" and f["data"]["message"]["display_name"] == "helper"
            for f in frames
        )
    assert mock_runtime.calls == []


# ---------------------------------------------------------------------------
# Multi-agent + loop guard (phase 4)
# ---------------------------------------------------------------------------


async def test_two_agents_can_be_addressed_independently(
    server: str, client: httpx.AsyncClient, mock_runtime: MockAgentRuntime
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    await _make_agent(
        client, pid, alice["member_token"], name="alpha", trigger="on_mention"
    )
    await _make_agent(
        client, pid, alice["member_token"], name="beta", trigger="on_mention"
    )
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws, "register_api_key", api_key="sk-test")
        await recv_frame(ws, "api_key_registered")

        await send(ws, "send_message", channel_id=general_id, content="@alpha go")
        await recv_frame(ws, "message")
        reply = await recv_frame(ws, "message")
        assert reply["data"]["message"]["display_name"] == "alpha"

        await send(ws, "send_message", channel_id=general_id, content="@beta go")
        await recv_frame(ws, "message")
        reply = await recv_frame(ws, "message")
        assert reply["data"]["message"]["display_name"] == "beta"


async def test_agent_can_mention_other_agent_to_chain(
    server: str, client: httpx.AsyncClient
) -> None:
    """If agent A's reply contains @B, agent B should pick it up — until the
    loop guard fires."""
    # Custom runtime: alpha mentions beta; beta mentions alpha. Loop until
    # the guard fires.
    class PingPongRuntime:
        def __init__(self) -> None:
            self.calls: list = []

        async def run(self, invocation):
            self.calls.append(invocation)
            if invocation.agent_display_name == "alpha":
                return "@beta your turn"
            return "@alpha your turn"

    runtime = PingPongRuntime()
    set_runtime_override(runtime)
    try:
        alice = await create_project(client, "demo", "alice")
        pid = alice["project_id"]
        await _make_agent(
            client, pid, alice["member_token"], name="alpha", trigger="on_mention"
        )
        await _make_agent(
            client, pid, alice["member_token"], name="beta", trigger="on_mention"
        )
        async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
            hello = await recv_frame(ws, "hello")
            general_id = hello["data"]["channels"][0]["id"]
            await send(ws, "register_api_key", api_key="sk-test")
            await recv_frame(ws, "api_key_registered")
            await send(ws, "send_message", channel_id=general_id, content="@alpha start")
            # Alice's own message echoes back.
            await recv_frame(ws, "message")
            # Drain until things go quiet.
            frames = await drain(ws, quiet_for=0.5)
            agent_msgs = [
                f for f in frames if f.get("type") == "message"
                and f["data"]["message"]["display_name"] in {"alpha", "beta"}
            ]
            # The loop guard caps consecutive agent-only messages at 4.
            assert 1 <= len(agent_msgs) <= 4, f"got {len(agent_msgs)} agent messages"
            # The agents must alternate (last is whichever was triggered most recently).
            speakers = [m["data"]["message"]["display_name"] for m in agent_msgs]
            for i in range(1, len(speakers)):
                assert speakers[i] != speakers[i - 1]
    finally:
        set_runtime_override(None)


async def test_human_message_resets_loop_guard(
    server: str, client: httpx.AsyncClient
) -> None:
    """After the loop guard fires, a human message must un-stick it."""
    class AlwaysReplyRuntime:
        async def run(self, invocation):
            return "ack"

    set_runtime_override(AlwaysReplyRuntime())
    try:
        alice = await create_project(client, "demo", "alice")
        pid = alice["project_id"]
        await _make_agent(
            client, pid, alice["member_token"], name="echo", trigger="always"
        )
        async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
            hello = await recv_frame(ws, "hello")
            general_id = hello["data"]["channels"][0]["id"]
            await send(ws, "register_api_key", api_key="sk-test")
            await recv_frame(ws, "api_key_registered")
            # Trigger a chain that fills the guard.
            await send(ws, "send_message", channel_id=general_id, content="start")
            await drain(ws, quiet_for=0.6)
            # Now a fresh human message: agent should respond again (streak reset).
            await send(ws, "send_message", channel_id=general_id, content="round two")
            frames = await drain(ws, quiet_for=0.6)
            agent_replies = [
                f for f in frames if f.get("type") == "message"
                and f["data"]["message"]["display_name"] == "echo"
            ]
            assert agent_replies, "human reset should have re-enabled the agent"
    finally:
        set_runtime_override(None)


async def test_agent_runtime_failure_does_not_crash_server(
    server: str, client: httpx.AsyncClient
) -> None:
    """If the runtime raises, the dispatcher logs and continues."""
    class BrokenRuntime:
        async def run(self, invocation):
            raise RuntimeError("transport broken")

    set_runtime_override(BrokenRuntime())
    try:
        alice = await create_project(client, "demo", "alice")
        pid = alice["project_id"]
        await _make_agent(
            client, pid, alice["member_token"], name="flaky", trigger="on_mention"
        )
        async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
            hello = await recv_frame(ws, "hello")
            general_id = hello["data"]["channels"][0]["id"]
            await send(ws, "register_api_key", api_key="sk-test")
            await recv_frame(ws, "api_key_registered")
            await send(ws, "send_message", channel_id=general_id, content="@flaky hi")
            await recv_frame(ws, "message")  # alice's message
            # WS stays alive; ping/pong still works after the agent failure.
            await asyncio.sleep(0.2)
            await send(ws, "ping")
            await recv_frame(ws, "pong")
    finally:
        set_runtime_override(None)


async def test_channel_scoped_agent_ignores_other_channels(
    server: str, client: httpx.AsyncClient, mock_runtime: MockAgentRuntime
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws, "create_channel", name="private")
        priv_id = (await recv_frame(ws, "channel_created"))["data"]["channel"]["id"]
        await _make_agent(
            client, pid, alice["member_token"],
            name="scoped", trigger="always", channel_id=priv_id,
        )
        await recv_frame(ws, "member_joined")
        await recv_frame(ws, "agent_created")
        await send(ws, "register_api_key", api_key="sk-test")
        await recv_frame(ws, "api_key_registered")

        # Post in #general — scoped agent must NOT respond.
        await send(ws, "send_message", channel_id=general_id, content="anyone home?")
        await recv_frame(ws, "message")
        frames = await drain(ws, quiet_for=0.3)
        assert not any(
            f.get("type") == "message" and f["data"]["message"]["display_name"] == "scoped"
            for f in frames
        )
        # Post in #private — scoped agent must respond.
        await send(ws, "send_message", channel_id=priv_id, content="hello scoped")
        await recv_frame(ws, "message")
        reply = await recv_frame(ws, "message")
        assert reply["data"]["message"]["display_name"] == "scoped"


async def test_empty_agent_reply_is_dropped(
    server: str, client: httpx.AsyncClient
) -> None:
    """An agent that returns an empty string posts nothing."""
    class SilentRuntime:
        async def run(self, invocation):
            return ""

    set_runtime_override(SilentRuntime())
    try:
        alice = await create_project(client, "demo", "alice")
        pid = alice["project_id"]
        await _make_agent(
            client, pid, alice["member_token"], name="quiet", trigger="always"
        )
        async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
            hello = await recv_frame(ws, "hello")
            general_id = hello["data"]["channels"][0]["id"]
            await send(ws, "register_api_key", api_key="sk-test")
            await recv_frame(ws, "api_key_registered")
            await send(ws, "send_message", channel_id=general_id, content="hi")
            await recv_frame(ws, "message")
            frames = await drain(ws, quiet_for=0.3)
            assert not any(
                f.get("type") == "message" and f["data"]["message"]["display_name"] == "quiet"
                for f in frames
            )
    finally:
        set_runtime_override(None)
