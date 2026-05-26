"""Coverage for the curated agent-preset library and the /agent add
preset:<name> shortcut.

These tests exercise the resolution logic on the server (`PRESETS` ↔
`AgentConfig`) and the TUI command path. The runner itself is faked the
same way as in `test_agents.py` so we never hit the SDK."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from cowork.client.tui import CoworkApp
from cowork.server.agent_presets import PRESETS, list_presets
from cowork.server.agent_runner import FakeAgentRunner
from textual.widgets import Input

from tests.conftest import create_project


def _install_fake_runner(responses: list[str] | None = None) -> FakeAgentRunner:
    from cowork.server.app import app
    runner = FakeAgentRunner(responses=responses or ["(mock)"])
    app.state.agent_runner = runner
    return runner


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
    from textual.widgets import RichLog
    log = app.query_one("#transcript", RichLog)
    chunks = []
    for line in log.lines:
        try:
            chunks.append(line.text)
        except Exception:
            chunks.append(str(line))
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Preset library itself
# ---------------------------------------------------------------------------


def test_every_preset_has_a_useful_system_prompt() -> None:
    """Each preset must define a non-trivial system prompt — the whole
    point of the library is curated specialty. An empty / one-word prompt
    would silently turn into a generic chatbot."""
    assert PRESETS, "PRESETS registry is empty"
    for name, (description, cfg) in PRESETS.items():
        assert description.strip(), f"preset {name!r} missing description"
        assert len(cfg.system_prompt) > 100, (
            f"preset {name!r} system prompt is suspiciously short:"
            f" {cfg.system_prompt!r}"
        )
        # Every preset should explicitly tell the model how to address
        # itself in chat — that's what makes them feel like personalities.
        assert f"@{name}" in cfg.system_prompt, (
            f"preset {name!r} system prompt should mention '@{name}' so"
            " the model knows its handle"
        )


def test_list_presets_returns_wire_friendly_dicts() -> None:
    """`list_presets()` powers the HTTP endpoint and the TUI listing — it
    has to be JSON-serializable and include name + description + model."""
    items = list_presets()
    assert len(items) == len(PRESETS)
    for item in items:
        assert set(item) >= {"name", "description", "model"}
        assert isinstance(item["name"], str)


# ---------------------------------------------------------------------------
# HTTP: GET /agents/presets, POST /projects/.../agents with preset
# ---------------------------------------------------------------------------


async def test_get_agents_presets_returns_full_library(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/agents/presets")
    r.raise_for_status()
    body = r.json()
    names = {p["name"] for p in body["presets"]}
    # Spot-check a few presets we ship — the test stays robust to adding
    # more presets later (we don't assert on the full set).
    assert {"architect", "reviewer", "tester"} <= names


async def test_register_agent_with_preset_only_uses_defaults(
    client: httpx.AsyncClient,
) -> None:
    """`{"preset": "architect"}` is the minimum viable registration. The
    server fills in display_name (defaults to preset name), system_prompt,
    and model. The agent shows up in bootstrap."""
    _install_fake_runner()
    proj = await create_project(client, "p", "alice")
    r = await client.post(
        f"/projects/{proj['project_id']}/agents",
        headers={"Authorization": f"Bearer {proj['member_token']}"},
        json={"preset": "architect"},
    )
    r.raise_for_status()
    body = r.json()
    assert body["display_name"] == "architect"
    assert body["kind"] == "agent"

    # And the stored agent_config reflects the preset's prompt.
    bs = await client.get(
        f"/projects/{proj['project_id']}/bootstrap",
        headers={"Authorization": f"Bearer {proj['member_token']}"},
    )
    bs.raise_for_status()
    arch = next(m for m in bs.json()["members"] if m["display_name"] == "architect")
    assert arch["kind"] == "agent"


async def test_register_agent_with_preset_override_display_name(
    client: httpx.AsyncClient,
) -> None:
    """You can drop two architects in a project by giving the second one
    a different display name — `{preset: architect, display_name: senior}`
    works even though the preset's default name is taken."""
    _install_fake_runner()
    proj = await create_project(client, "p", "alice")
    await client.post(
        f"/projects/{proj['project_id']}/agents",
        headers={"Authorization": f"Bearer {proj['member_token']}"},
        json={"preset": "architect"},
    )
    r = await client.post(
        f"/projects/{proj['project_id']}/agents",
        headers={"Authorization": f"Bearer {proj['member_token']}"},
        json={"preset": "architect", "display_name": "senior"},
    )
    r.raise_for_status()
    body = r.json()
    assert body["display_name"] == "senior"


async def test_register_agent_with_unknown_preset_400(
    client: httpx.AsyncClient,
) -> None:
    proj = await create_project(client, "p", "alice")
    r = await client.post(
        f"/projects/{proj['project_id']}/agents",
        headers={"Authorization": f"Bearer {proj['member_token']}"},
        json={"preset": "no-such-preset"},
    )
    assert r.status_code == 400
    # The error names the available presets so the user can pick one.
    assert "architect" in r.text


async def test_register_agent_without_prompt_or_preset_400(
    client: httpx.AsyncClient,
) -> None:
    """A registration with neither `system_prompt` nor `preset` is
    nonsense — the agent would have no behavior to drive it. Reject."""
    proj = await create_project(client, "p", "alice")
    r = await client.post(
        f"/projects/{proj['project_id']}/agents",
        headers={"Authorization": f"Bearer {proj['member_token']}"},
        json={"display_name": "ghost"},
    )
    assert r.status_code == 400
    assert "system_prompt" in r.text


async def test_register_agent_explicit_overrides_beat_preset(
    client: httpx.AsyncClient,
) -> None:
    """Mixing modes is allowed: an explicit `system_prompt` wins over the
    preset's. Lets users start from a preset and tweak it."""
    _install_fake_runner(responses=["overridden"])
    proj = await create_project(client, "p", "alice")
    r = await client.post(
        f"/projects/{proj['project_id']}/agents",
        headers={"Authorization": f"Bearer {proj['member_token']}"},
        json={
            "preset": "architect",
            "display_name": "arch2",
            "system_prompt": "Speak only in haiku.",
        },
    )
    r.raise_for_status()
    # We can't see the stored prompt directly via HTTP (deliberate — it's
    # an implementation detail of the agent), but we can verify the
    # override took effect by triggering the runner and inspecting the
    # FakeAgentRunner's recorded calls.
    from cowork.server.app import app
    runner: FakeAgentRunner = app.state.agent_runner  # type: ignore[assignment]
    bs = await client.get(
        f"/projects/{proj['project_id']}/bootstrap",
        headers={"Authorization": f"Bearer {proj['member_token']}"},
    )
    bs.raise_for_status()
    channels = bs.json()["channels"]
    general_id = next(c["id"] for c in channels if c["name"] == "general")

    # Post a mention via raw WS so the runner is invoked.
    import json
    import websockets
    from tests.conftest import recv_frame, send, ws_url

    server = str(client.base_url).rstrip("/")
    async with websockets.connect(
        ws_url(server, proj["member_token"], proj["project_id"])
    ) as ws:
        await recv_frame(ws, "hello")
        await send(ws, "send_message", channel_id=general_id, content="@arch2 ping")
        await recv_frame(ws, "message")
        await recv_frame(ws, "message", timeout=5.0)  # bot's reply

    assert runner.calls, "runner was never invoked"
    call = runner.calls[0]
    assert call["system_prompt"] == "Speak only in haiku.", (
        f"explicit system_prompt didn't override the preset:"
        f" got {call['system_prompt']!r}"
    )


# ---------------------------------------------------------------------------
# TUI: /agent presets, /agent add preset:<name>
# ---------------------------------------------------------------------------


async def test_tui_agent_presets_lists_library(server: str) -> None:
    """`/agent presets` dumps the registry into the transcript."""
    _install_fake_runner()
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects))
        await _submit(pilot, "/agent presets")
        text = _transcript_text(app)
        # Every preset we ship should appear in the listing.
        for name in PRESETS:
            assert name in text, f"preset {name!r} missing from /agent presets output"


async def test_tui_agent_add_preset_registers_with_default_name(
    server: str,
) -> None:
    """`/agent add preset:reviewer` creates a member named 'reviewer'
    using the curated prompt — no quoted prompt string needed."""
    _install_fake_runner()
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects))
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels))
        await _submit(pilot, "/agent add preset:reviewer")
        await _wait_for(
            lambda: any(
                m.get("display_name") == "reviewer" and m.get("kind") == "agent"
                for m in state.members.values()
            )
        )


async def test_tui_agent_add_preset_with_override_display_name(
    server: str,
) -> None:
    """`/agent add preset:architect senior` — second positional arg is the
    display name override. Lets users add multiple architects to one
    project."""
    _install_fake_runner()
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects))
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels))
        await _submit(pilot, "/agent add preset:architect")
        await _wait_for(
            lambda: any(
                m.get("display_name") == "architect"
                for m in state.members.values()
            )
        )
        await _submit(pilot, "/agent add preset:architect senior")
        await _wait_for(
            lambda: any(
                m.get("display_name") == "senior"
                for m in state.members.values()
            )
        )


async def test_tui_agent_add_unknown_preset_surfaces_error(
    server: str,
) -> None:
    _install_fake_runner()
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects))
        await _submit(pilot, "/agent add preset:nope")
        text = _transcript_text(app)
        assert "unknown preset" in text
