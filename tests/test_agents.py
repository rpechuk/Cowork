"""End-to-end coverage for agent membership + invocation.

The Claude Agent SDK is never actually called — the suite swaps in
`FakeAgentRunner` after the server starts so we can verify message flow,
mention detection, and broadcasting without an API key or the `claude`
CLI being installed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import websockets

from cowork.client.tui import CoworkApp
from cowork.server.agent_runner import FakeAgentRunner
from textual.widgets import Input

from tests.conftest import (
    assert_no_frame,
    create_project,
    drain,
    recv_frame,
    redeem_invite,
    send,
    ws_url,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _install_fake_runner(responses: list[str] | None = None) -> FakeAgentRunner:
    """Replace the server's default ClaudeSDKAgentRunner with a fake that
    returns canned responses. Returns the fake so individual tests can
    assert on its recorded calls."""
    from cowork.server.app import app

    runner = FakeAgentRunner(responses=responses or ["(mock reply)"])
    app.state.agent_runner = runner
    return runner


async def _register_agent(
    client: httpx.AsyncClient,
    member_token: str,
    project_id: str,
    *,
    display_name: str = "bot",
    system_prompt: str = "You are a helpful assistant.",
    model: str = "claude-sonnet-4-6",
) -> dict:
    r = await client.post(
        f"/projects/{project_id}/agents",
        headers={"Authorization": f"Bearer {member_token}"},
        json={
            "display_name": display_name,
            "system_prompt": system_prompt,
            "model": model,
        },
    )
    r.raise_for_status()
    return r.json()


async def _submit(pilot, text: str) -> None:
    inp = pilot.app.query_one("#input", Input)
    inp.value = text
    inp.cursor_position = len(text)
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


async def _wait_for(condition, timeout: float = 5.0, interval: float = 0.05) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


# ---------------------------------------------------------------------------
# HTTP registration
# ---------------------------------------------------------------------------


async def test_register_agent_creates_member_with_kind_agent(
    client: httpx.AsyncClient,
) -> None:
    """POSTing to /projects/{id}/agents creates a new project member with
    kind='agent', and that agent shows up in subsequent bootstrap responses
    alongside the human members."""
    _install_fake_runner()
    proj = await create_project(client, "p", "alice")

    res = await _register_agent(
        client, proj["member_token"], proj["project_id"],
        display_name="reviewer",
        system_prompt="You review code.",
    )
    assert res["display_name"] == "reviewer"
    assert res["kind"] == "agent"
    assert res["member_id"]

    bs = await client.get(
        f"/projects/{proj['project_id']}/bootstrap",
        headers={"Authorization": f"Bearer {proj['member_token']}"},
    )
    bs.raise_for_status()
    members = bs.json()["members"]
    by_name = {m["display_name"]: m for m in members}
    assert by_name["alice"]["kind"] == "human"
    assert by_name["reviewer"]["kind"] == "agent"
    # Agents don't hold WS connections so the presence overlay would mark
    # them offline — but we exempt them. They should report whatever the
    # DB has (default 'online' on insert).
    assert by_name["reviewer"]["status"] == "online"


async def test_register_agent_rejects_duplicate_display_name(
    client: httpx.AsyncClient,
) -> None:
    """Display name uniqueness is enforced for agents the same way it is
    for humans — alice can't have a same-named agent shadow her."""
    _install_fake_runner()
    proj = await create_project(client, "p", "alice")
    r = await client.post(
        f"/projects/{proj['project_id']}/agents",
        headers={"Authorization": f"Bearer {proj['member_token']}"},
        json={
            "display_name": "alice",  # collides with the human creator
            "system_prompt": "hi",
        },
    )
    assert r.status_code == 400
    assert "already taken" in r.text


async def test_register_agent_requires_project_membership(
    client: httpx.AsyncClient,
) -> None:
    """Bearer token authn: someone holding a member token for a different
    project can't drop agents into ours."""
    _install_fake_runner()
    proj_a = await create_project(client, "a", "alice")
    proj_b = await create_project(client, "b", "bob")
    r = await client.post(
        f"/projects/{proj_a['project_id']}/agents",
        headers={"Authorization": f"Bearer {proj_b['member_token']}"},
        json={"display_name": "intruder", "system_prompt": "hi"},
    )
    assert r.status_code == 403


async def test_delete_agent_removes_it_and_broadcasts(
    server: str, client: httpx.AsyncClient
) -> None:
    """DELETE /projects/{id}/agents/{agent_id} tears the agent down. Live
    sockets in the project see a member_left frame so their members
    panels can update without re-bootstrapping."""
    _install_fake_runner()
    proj = await create_project(client, "p", "alice")
    agent = await _register_agent(
        client, proj["member_token"], proj["project_id"],
        display_name="bot",
        system_prompt="hi",
    )
    # Open alice's WS so the broadcast has somewhere to land.
    async with websockets.connect(
        ws_url(server, proj["member_token"], proj["project_id"])
    ) as ws:
        await recv_frame(ws, "hello")

        r = await client.delete(
            f"/projects/{proj['project_id']}/agents/{agent['member_id']}",
            headers={"Authorization": f"Bearer {proj['member_token']}"},
        )
        assert r.status_code == 204

        frame = await recv_frame(ws, "member_left")
        assert frame["data"]["member_id"] == agent["member_id"]

    # Bootstrap no longer lists the agent.
    bs = await client.get(
        f"/projects/{proj['project_id']}/bootstrap",
        headers={"Authorization": f"Bearer {proj['member_token']}"},
    )
    bs.raise_for_status()
    names = {m["display_name"] for m in bs.json()["members"]}
    assert "bot" not in names


async def test_delete_agent_refuses_to_remove_humans(
    client: httpx.AsyncClient,
) -> None:
    """The endpoint is agent-only; using it to drop a human member is
    rejected. Human kick is a separate (not-yet-built) flow."""
    _install_fake_runner()
    proj = await create_project(client, "p", "alice")
    r = await client.delete(
        f"/projects/{proj['project_id']}/agents/{proj['member_id']}",
        headers={"Authorization": f"Bearer {proj['member_token']}"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Invocation on @mention
# ---------------------------------------------------------------------------


async def test_at_mention_invokes_agent_runner_and_posts_reply(
    server: str, client: httpx.AsyncClient
) -> None:
    """End-to-end: alice posts '@bot what time is it?' in #general, the
    runner records the call (including the conversation history and the
    invoker's name), and the bot's reply lands as a real message frame
    attributed to the agent."""
    runner = _install_fake_runner(responses=["it's chat o'clock"])
    proj = await create_project(client, "p", "alice")
    agent = await _register_agent(
        client, proj["member_token"], proj["project_id"],
        display_name="bot",
        system_prompt="You are a clock.",
    )

    async with websockets.connect(
        ws_url(server, proj["member_token"], proj["project_id"])
    ) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = next(
            c["id"] for c in hello["data"]["channels"] if c["name"] == "general"
        )
        await send(
            ws, "send_message",
            channel_id=general_id,
            content="@bot what time is it?",
        )
        # Alice's own message echoes back first.
        own = await recv_frame(ws, "message")
        assert own["data"]["message"]["content"] == "@bot what time is it?"

        # Then the agent's reply comes through as another `message` frame
        # whose author is the bot, not alice.
        reply = await recv_frame(ws, "message", timeout=5.0)
        assert reply["data"]["message"]["display_name"] == "bot"
        assert reply["data"]["message"]["content"] == "it's chat o'clock"
        assert reply["data"]["message"]["member_id"] == agent["member_id"]

    # Runner saw exactly one call, with the right system prompt + invoker.
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["agent"] == "bot"
    assert call["invoker"] == "alice"
    assert call["system_prompt"] == "You are a clock."
    # The most recent history entry was alice's @mention message itself.
    assert call["history"][-1] == "@bot what time is it?"


async def test_agent_thinking_and_done_frames_bracket_the_reply(
    server: str, client: httpx.AsyncClient
) -> None:
    """The server announces agent_thinking before invoking the runner and
    agent_done after — TUIs use these to drive a typing indicator."""
    _install_fake_runner(responses=["...thinking done"])
    proj = await create_project(client, "p", "alice")
    await _register_agent(
        client, proj["member_token"], proj["project_id"],
        display_name="bot",
        system_prompt="hi",
    )
    async with websockets.connect(
        ws_url(server, proj["member_token"], proj["project_id"])
    ) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = next(
            c["id"] for c in hello["data"]["channels"] if c["name"] == "general"
        )
        await send(ws, "send_message", channel_id=general_id, content="@bot ping")
        # Drain everything for ~1s and confirm we saw both bracketing
        # frames in the right order.
        seen: list[str] = []
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
            except asyncio.TimeoutError:
                if "agent_done" in seen:
                    break
                continue
            seen.append(json.loads(raw).get("type", "?"))
            if seen.count("agent_done") >= 1:
                break
    # The agent_thinking must precede agent_done; the bot's `message`
    # frame appears between them.
    assert "agent_thinking" in seen
    assert "agent_done" in seen
    assert seen.index("agent_thinking") < seen.index("agent_done")


async def test_human_not_mentioned_does_not_trigger_runner(
    server: str, client: httpx.AsyncClient
) -> None:
    """Sanity: a regular message that doesn't name the agent must NOT
    invoke the runner. Agents only respond when @-mentioned."""
    runner = _install_fake_runner()
    proj = await create_project(client, "p", "alice")
    await _register_agent(
        client, proj["member_token"], proj["project_id"],
        display_name="bot",
        system_prompt="hi",
    )
    async with websockets.connect(
        ws_url(server, proj["member_token"], proj["project_id"])
    ) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = next(
            c["id"] for c in hello["data"]["channels"] if c["name"] == "general"
        )
        await send(ws, "send_message", channel_id=general_id, content="hello world")
        # Wait long enough for any spurious invocation to land.
        await asyncio.sleep(0.4)
    assert runner.calls == [], (
        f"runner was invoked without an @mention: {runner.calls!r}"
    )


async def test_agent_at_here_broadcast_triggers_agent_response(
    server: str, client: httpx.AsyncClient
) -> None:
    """@here / @channel resolve to every member, including agents. An agent
    in the project should respond to a broadcast just like a named ping."""
    runner = _install_fake_runner(responses=["i'm here"])
    proj = await create_project(client, "p", "alice")
    await _register_agent(
        client, proj["member_token"], proj["project_id"],
        display_name="bot",
        system_prompt="hi",
    )
    async with websockets.connect(
        ws_url(server, proj["member_token"], proj["project_id"])
    ) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = next(
            c["id"] for c in hello["data"]["channels"] if c["name"] == "general"
        )
        await send(ws, "send_message", channel_id=general_id, content="@here check in")
        await recv_frame(ws, "message")  # alice's own
        reply = await recv_frame(ws, "message", timeout=5.0)
        assert reply["data"]["message"]["display_name"] == "bot"
    assert len(runner.calls) == 1


async def test_agent_failure_surfaces_as_agent_error_frame(
    server: str, client: httpx.AsyncClient
) -> None:
    """If the runner blows up, the server reports an `agent_error` frame to
    the channel so users see what happened instead of waiting forever for
    a reply that's never coming."""
    class _BoomRunner:
        async def respond(self, *a, **kw):
            raise RuntimeError("simulated SDK failure")

    from cowork.server.app import app
    app.state.agent_runner = _BoomRunner()

    proj = await create_project(client, "p", "alice")
    await _register_agent(
        client, proj["member_token"], proj["project_id"],
        display_name="bot",
        system_prompt="hi",
    )
    async with websockets.connect(
        ws_url(server, proj["member_token"], proj["project_id"])
    ) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = next(
            c["id"] for c in hello["data"]["channels"] if c["name"] == "general"
        )
        await send(ws, "send_message", channel_id=general_id, content="@bot hi")
        await recv_frame(ws, "message")
        err = await recv_frame(ws, "agent_error", timeout=5.0)
        assert "simulated SDK failure" in err["data"]["message"]
        assert err["data"]["agent_display_name"] == "bot"
        # agent_done still fires so any typing indicator clears.
        await recv_frame(ws, "agent_done", timeout=5.0)


# ---------------------------------------------------------------------------
# TUI /agent commands
# ---------------------------------------------------------------------------


async def test_tui_agent_add_command_registers_and_shows_agent(
    server: str,
) -> None:
    """/agent add <name> <prompt…> hits the server, the resulting
    member_joined broadcast updates the local members panel, and the
    agent shows up with kind='agent'."""
    _install_fake_runner()
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects))
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels))

        await _submit(
            pilot,
            '/agent add reviewer "You are a strict code reviewer."',
        )
        await _wait_for(
            lambda: any(
                m.get("display_name") == "reviewer" and m.get("kind") == "agent"
                for m in state.members.values()
            )
        )


async def test_tui_agent_list_renders_registered_agents(server: str) -> None:
    """/agent list dumps the registered agents into the transcript so the
    user can audit who's in the project without scanning the members
    panel."""
    _install_fake_runner()
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects))
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels))

        await _submit(
            pilot, '/agent add architect "You design system architecture."'
        )
        await _wait_for(
            lambda: any(
                m.get("display_name") == "architect"
                for m in state.members.values()
            )
        )
        await _submit(pilot, "/agent list")
        # Pull the transcript text and verify the bot name shows up.
        from textual.widgets import RichLog
        log = app.query_one("#transcript", RichLog)
        chunks = []
        for line in log.lines:
            try:
                chunks.append(line.text)
            except Exception:
                chunks.append(str(line))
        text = "\n".join(chunks)
        assert "Agents in this project" in text
        assert "@architect" in text


async def test_tui_agent_remove_drops_the_agent(server: str) -> None:
    """/agent remove tears down the agent and the member_left broadcast
    clears it from the local members map."""
    _install_fake_runner()
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects))
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels))

        await _submit(pilot, '/agent add bot "hello"')
        await _wait_for(
            lambda: any(
                m.get("display_name") == "bot"
                for m in state.members.values()
            )
        )
        await _submit(pilot, "/agent remove bot")
        await _wait_for(
            lambda: not any(
                m.get("display_name") == "bot"
                for m in state.members.values()
            )
        )


async def test_tui_round_trip_agent_response_appears_in_transcript(
    server: str,
) -> None:
    """The full user-visible flow: alice adds an agent, @-mentions it, the
    runner returns a canned reply, and the reply shows up in the
    transcript attributed to the agent — not to alice."""
    _install_fake_runner(responses=["I'd start with the data model."])
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects))
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels))

        await _submit(
            pilot, '/agent add architect "You design systems."'
        )
        await _wait_for(
            lambda: any(
                m.get("display_name") == "architect"
                for m in state.members.values()
            )
        )
        await _submit(pilot, "@architect where should I start?")
        # Wait for any message authored by 'architect' to land in the
        # current channel.
        await _wait_for(
            lambda: any(
                m.get("display_name") == "architect"
                for m in state.messages_by_channel.get(
                    app.current_channel_id, []
                )
            )
        )
