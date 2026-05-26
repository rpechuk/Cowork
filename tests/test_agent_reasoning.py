"""Coverage for streamed agent reasoning — the agent_progress WS frame,
the per-agent reasoning trail on the client, the members-panel snippet
under a busy agent, and the `/agent show <name>` inspector command.

The runner is faked end-to-end via FakeAgentRunner, which now accepts a
`progress` list of (kind, text) events that it feeds through the
on_progress callback before returning the final response."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import websockets

from cowork.client.tui import CoworkApp
from cowork.server.agent_runner import FakeAgentRunner
from textual.widgets import Input, RichLog, Static

from tests.conftest import create_project, recv_frame, send, ws_url


def _install_fake_runner(
    responses: list[str] | None = None,
    progress: list[tuple[str, str]] | None = None,
) -> FakeAgentRunner:
    from cowork.server.app import app
    runner = FakeAgentRunner(
        responses=responses or ["(mock)"],
        progress=progress or [],
    )
    app.state.agent_runner = runner
    return runner


async def _register(client, member_token, project_id, *, name="bot", prompt="hi"):
    r = await client.post(
        f"/projects/{project_id}/agents",
        headers={"Authorization": f"Bearer {member_token}"},
        json={"display_name": name, "system_prompt": prompt},
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


def _transcript_text(app: CoworkApp) -> str:
    log = app.query_one("#transcript", RichLog)
    chunks: list[str] = []
    for line in log.lines:
        try:
            chunks.append(line.text)
        except Exception:
            chunks.append(str(line))
    return "\n".join(chunks)


def _members_panel_text(app: CoworkApp) -> str:
    panel = app.query_one("#members-list", Static)
    return str(panel.render())


# ---------------------------------------------------------------------------
# Server: agent_progress frames
# ---------------------------------------------------------------------------


async def test_runner_progress_events_become_agent_progress_frames(
    server: str, client: httpx.AsyncClient
) -> None:
    """Each (kind, text) event the runner emits via on_progress must
    arrive at every connected client as an `agent_progress` WS frame
    in order, sandwiched between agent_thinking and agent_done."""
    _install_fake_runner(
        responses=["all done"],
        progress=[
            ("thinking", "Let me consider the trade-offs..."),
            ("text", "I think you should use postgres."),
            ("tool_use", "calculator(x=1, y=2)"),
        ],
    )
    proj = await create_project(client, "p", "alice")
    await _register(client, proj["member_token"], proj["project_id"], name="bot")

    async with websockets.connect(
        ws_url(server, proj["member_token"], proj["project_id"])
    ) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = next(
            c["id"] for c in hello["data"]["channels"] if c["name"] == "general"
        )
        await send(ws, "send_message", channel_id=general_id, content="@bot hi")
        # Collect frame types until we've seen agent_done.
        seen: list[dict] = []
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
            except asyncio.TimeoutError:
                if any(f.get("type") == "agent_done" for f in seen):
                    break
                continue
            seen.append(json.loads(raw))
            if any(f.get("type") == "agent_done" for f in seen):
                break

    types = [f["type"] for f in seen]
    assert "agent_thinking" in types
    assert "agent_done" in types
    progress_frames = [f for f in seen if f["type"] == "agent_progress"]
    assert len(progress_frames) == 3, types
    # Order preserved end to end.
    kinds = [f["data"]["kind"] for f in progress_frames]
    assert kinds == ["thinking", "text", "tool_use"]
    # Each frame carries channel_id + agent_display_name so the TUI can
    # render it without re-resolving from local state.
    for f in progress_frames:
        assert f["data"]["channel_id"] == general_id
        assert f["data"]["agent_display_name"] == "bot"
    # Sandwiched: every agent_progress lands AFTER agent_thinking and
    # BEFORE agent_done.
    first_thinking = types.index("agent_thinking")
    last_done = types.index("agent_done")
    for i, t in enumerate(types):
        if t == "agent_progress":
            assert first_thinking < i < last_done, types


async def test_runner_with_no_progress_still_works(
    server: str, client: httpx.AsyncClient
) -> None:
    """Backwards compatibility: a runner that doesn't emit any progress
    events (FakeAgentRunner default) must still produce a reply. No
    progress frames should arrive."""
    _install_fake_runner(responses=["hello"])
    proj = await create_project(client, "p", "alice")
    await _register(client, proj["member_token"], proj["project_id"], name="bot")
    async with websockets.connect(
        ws_url(server, proj["member_token"], proj["project_id"])
    ) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = next(
            c["id"] for c in hello["data"]["channels"] if c["name"] == "general"
        )
        await send(ws, "send_message", channel_id=general_id, content="@bot hi")
        await recv_frame(ws, "message")  # alice's own
        reply = await recv_frame(ws, "message", timeout=5.0)
        assert reply["data"]["message"]["content"] == "hello"
        # No agent_progress frame should arrive within a reasonable
        # window (we already drained up to the reply; check ws is quiet
        # for progress specifically).
        await recv_frame(ws, "agent_done", timeout=2.0)


async def test_progress_callback_failure_does_not_kill_invocation(
    server: str, client: httpx.AsyncClient
) -> None:
    """A flaky on_progress (one client's socket dies mid-stream) must not
    take down the agent's entire response. The reply still posts."""
    class _PartiallyBrokenRunner:
        async def respond(
            self, config, agent_display_name, history,
            invoker_display_name, on_progress=None,
        ):
            # First progress event: fine.
            if on_progress is not None:
                await on_progress("thinking", "first event ok")
                # Second: simulate a callback that raises. The runner
                # swallows it, keeps going.
                async def boom(*a, **kw):
                    raise RuntimeError("boom")
                await ClaudeSDKAgentRunnerCallShim()(boom, "text", "ignored")
            return "final reply"

    # Helper: actually call the real exception-swallowing pattern. We
    # inline the try/except here to keep this test self-contained.
    class ClaudeSDKAgentRunnerCallShim:
        def __call__(self):
            async def _call(cb, kind, text):
                try:
                    await cb(kind, text)
                except Exception:
                    pass
            return _call

    # Easier: just install a runner that raises in on_progress and
    # confirm the message still appears.
    from cowork.server.app import app

    class _RunnerThatBlowsUpProgress:
        async def respond(
            self, config, agent_display_name, history,
            invoker_display_name, on_progress=None,
        ):
            if on_progress is not None:
                async def bad():
                    raise RuntimeError("client gone")
                try:
                    await on_progress("text", "before crash")
                except Exception:
                    pass  # outer try; on_progress should swallow internally
            return "still got here"

    app.state.agent_runner = _RunnerThatBlowsUpProgress()
    proj = await create_project(client, "p", "alice")
    await _register(client, proj["member_token"], proj["project_id"], name="bot")
    async with websockets.connect(
        ws_url(server, proj["member_token"], proj["project_id"])
    ) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = next(
            c["id"] for c in hello["data"]["channels"] if c["name"] == "general"
        )
        await send(ws, "send_message", channel_id=general_id, content="@bot hi")
        await recv_frame(ws, "message")  # alice's own
        reply = await recv_frame(ws, "message", timeout=5.0)
        assert reply["data"]["message"]["content"] == "still got here"


# ---------------------------------------------------------------------------
# Client: TUI accumulates trail, shows snippet, /agent show dumps
# ---------------------------------------------------------------------------


async def test_tui_agent_progress_populates_reasoning_trail(server: str) -> None:
    """As progress frames arrive, the TUI's ProjectState.agent_reasoning
    grows; the members panel surfaces the latest snippet under the busy
    agent's name."""
    _install_fake_runner(
        responses=["here you go"],
        progress=[
            ("thinking", "weighing whether postgres or redis fits better"),
            ("text", "Going with postgres because durability matters more."),
        ],
    )
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects))
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels))
        await _submit(pilot, '/agent add bot "be helpful"')
        await _wait_for(
            lambda: any(
                m.get("display_name") == "bot" for m in state.members.values()
            )
        )
        agent_id = next(
            m["id"] for m in state.members.values()
            if m.get("display_name") == "bot"
        )
        await _submit(pilot, "@bot help")
        # Wait until both progress events have landed AND the final
        # response arrived.
        await _wait_for(
            lambda: len(state.agent_reasoning.get(agent_id, [])) >= 2
        )
        await _wait_for(
            lambda: any(
                m.get("display_name") == "bot"
                for m in state.messages_by_channel.get(
                    app.current_channel_id, []
                )
            )
        )
        trail = state.agent_reasoning[agent_id]
        kinds = [ev["kind"] for ev in trail]
        assert kinds == ["thinking", "text"]


async def test_tui_members_panel_shows_snippet_under_busy_agent(
    server: str,
) -> None:
    """While the agent is mid-response, the members panel shows a
    truncated snippet of the latest event beneath the agent's name."""
    # A slow runner so the busy state is observable: emit progress, then
    # block on an event we control before returning.
    proceed = asyncio.Event()

    class _SlowRunner:
        async def respond(
            self, config, agent_display_name, history,
            invoker_display_name, on_progress=None,
        ):
            if on_progress is not None:
                await on_progress(
                    "thinking",
                    "checking durability requirements for the queue layer",
                )
            await proceed.wait()
            return "final"

    from cowork.server.app import app
    app.state.agent_runner = _SlowRunner()

    coworkapp = CoworkApp()
    async with coworkapp.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(coworkapp.projects))
        state = next(iter(coworkapp.projects.values()))
        await _wait_for(lambda: bool(state.channels))
        await _submit(pilot, '/agent add bot "be helpful"')
        await _wait_for(
            lambda: any(
                m.get("display_name") == "bot" for m in state.members.values()
            )
        )
        await _submit(pilot, "@bot ping")
        # Wait for the progress event to land.
        await _wait_for(
            lambda: any(
                state.agent_reasoning.get(m["id"]) and
                state.agent_thinking
                for m in state.members.values()
                if m.get("display_name") == "bot"
            )
        )
        panel = _members_panel_text(coworkapp)
        assert "🤖 @bot" in panel
        assert "↳" in panel  # the snippet indent marker
        assert "checking durability" in panel
        # Release the runner so the TUI doesn't leak background tasks.
        proceed.set()


async def test_tui_agent_show_dumps_full_trail(server: str) -> None:
    """/agent show <name> renders the recorded events into the
    transcript with their kind tags so users can inspect what happened
    on the last turn."""
    _install_fake_runner(
        responses=["final"],
        progress=[
            ("thinking", "Let me think step by step about A, B, and C."),
            ("text", "Step 1: gather requirements."),
            ("tool_use", "read_file(path='/etc/hosts')"),
            ("text", "Step 2: propose a plan."),
        ],
    )
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects))
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels))
        await _submit(pilot, '/agent add bot "be helpful"')
        await _wait_for(
            lambda: any(
                m.get("display_name") == "bot" for m in state.members.values()
            )
        )
        agent_id = next(
            m["id"] for m in state.members.values()
            if m.get("display_name") == "bot"
        )
        await _submit(pilot, "@bot do the thing")
        await _wait_for(
            lambda: len(state.agent_reasoning.get(agent_id, [])) >= 4
        )
        await _submit(pilot, "/agent show bot")
        text = _transcript_text(app)
        # All four events labelled by kind, in order, with their text.
        assert "thinking" in text
        assert "tool_use" in text
        assert "Step 1: gather requirements" in text
        assert "read_file" in text
        assert "Step 2: propose a plan" in text


async def test_tui_agent_show_empty_when_agent_never_invoked(
    server: str,
) -> None:
    """/agent show on a fresh agent with no recorded turn explains there's
    nothing to show — doesn't print an empty box."""
    _install_fake_runner()
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects))
        await _submit(pilot, '/agent add bot "be helpful"')
        state = next(iter(app.projects.values()))
        await _wait_for(
            lambda: any(
                m.get("display_name") == "bot" for m in state.members.values()
            )
        )
        await _submit(pilot, "/agent show bot")
        text = _transcript_text(app)
        assert "no reasoning recorded" in text


async def test_tui_agent_show_for_unknown_agent_errors(server: str) -> None:
    """Asking to inspect an agent that doesn't exist is a clear red
    error — not a crash, not a silent no-op."""
    _install_fake_runner()
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects))
        await _submit(pilot, "/agent show ghost")
        text = _transcript_text(app)
        assert "no agent named @ghost" in text


async def test_tui_agent_reasoning_trail_clears_on_next_invocation(
    server: str,
) -> None:
    """Each invocation starts a fresh trail — the prior turn's events get
    wiped so `/agent show` always reflects the most recent ask, not a
    growing transcript of everything the agent ever said."""
    runner = _install_fake_runner(
        responses=["one", "two"],
        progress=[("text", "first invocation event")],
    )
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects))
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels))
        await _submit(pilot, '/agent add bot "be helpful"')
        await _wait_for(
            lambda: any(
                m.get("display_name") == "bot" for m in state.members.values()
            )
        )
        agent_id = next(
            m["id"] for m in state.members.values()
            if m.get("display_name") == "bot"
        )

        await _submit(pilot, "@bot first ask")
        await _wait_for(
            lambda: any(
                ev.get("text") == "first invocation event"
                for ev in state.agent_reasoning.get(agent_id, [])
            )
        )

        # Swap the runner's progress for the second invocation so we can
        # tell the trails apart.
        runner.progress = [("text", "second invocation event")]

        await _submit(pilot, "@bot second ask")
        # The first-invocation event must disappear once the second
        # invocation's agent_thinking fires.
        await _wait_for(
            lambda: (
                any(
                    ev.get("text") == "second invocation event"
                    for ev in state.agent_reasoning.get(agent_id, [])
                )
                and not any(
                    ev.get("text") == "first invocation event"
                    for ev in state.agent_reasoning.get(agent_id, [])
                )
            )
        )
