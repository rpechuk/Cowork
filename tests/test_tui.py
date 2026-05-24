"""End-to-end TUI integration tests.

These spin up a real in-process Cowork server (via the `server` fixture) and
drive the actual Textual TUI through `Pilot`. The point of these tests is to
catch the kinds of bugs that only manifest when the user actually opens the
app — keystrokes routed to the wrong widget, focus stuck on the project tree,
Ctrl+C colliding with terminal copy, etc.

They are slower than the pure-WS tests in `test_scenarios.py` (each takes
~1s for HTTP + WS round-trips), so we keep them small in number and focused
on the user-visible golden path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, RichLog

from cowork.client.tui import CoworkApp


async def _wait_for(condition, timeout: float = 3.0, interval: float = 0.05) -> None:
    """Poll `condition()` until truthy. Raises AssertionError on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


async def _submit(pilot, text: str) -> None:
    """Type `text` into the input box and press Enter, the way a user would."""
    inp = pilot.app.query_one("#input", Input)
    inp.value = text
    await pilot.press("enter")
    # Give async handlers a tick to run.
    await pilot.pause()


def _transcript_text(app: CoworkApp) -> str:
    """Concatenated visible transcript text — useful for substring asserts."""
    log = app.query_one("#transcript", RichLog)
    chunks = []
    for line in log.lines:
        try:
            chunks.append(line.text)
        except AttributeError:
            chunks.append(str(line))
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Connection: the bug that was blocking everything
# ---------------------------------------------------------------------------


async def test_input_has_focus_immediately_after_mount() -> None:
    """The regression: previously the Tree captured focus, silently swallowing
    every keystroke the user typed. Without this fix the user could never
    issue a /new-project command, so the app appeared 'broken'.
    """
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        focused = app.focused
        assert focused is not None
        assert focused.id == "input", (
            f"expected #input to have focus, got {focused!r}"
        )


async def test_user_can_create_project_via_slash_command(server: str) -> None:
    """Full golden path: type /new-project → server creates → bootstrap →
    WS connects → #general channel appears."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        # Wait for HTTP create + WS hello to settle.
        await _wait_for(lambda: len(app.projects) == 1, timeout=5.0)
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels), timeout=5.0)
        assert any(c["name"] == "general" for c in state.channels.values())
        assert app.current_project_id is not None
        assert app.current_channel_id is not None
        # The created-project banner shows the cowork:// URL.
        transcript = _transcript_text(app)
        assert "cowork://" in transcript
        assert "demo" in transcript


async def test_user_can_send_a_message_and_see_it_echo(server: str) -> None:
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        state_fn = lambda: next(iter(app.projects.values()), None)
        await _wait_for(lambda: state_fn() and state_fn().channels, timeout=5.0)
        # Now post a chat message. No leading slash → goes through _send_chat.
        await _submit(pilot, "hello world")
        await _wait_for(
            lambda: any(
                m["content"] == "hello world"
                for msgs in state_fn().messages_by_channel.values()
                for m in msgs
            ),
            timeout=5.0,
        )
        assert "hello world" in _transcript_text(app)


async def test_two_tui_clients_see_each_others_messages(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: Alice creates a project, prints an invite URL.
    Bob's TUI parses the URL via /join and joins. Both then exchange
    messages and see each other's text in their transcripts.
    """
    # Each TUI gets its own COWORK_HOME so caches don't collide.
    alice_home = tmp_path / "alice"
    alice_home.mkdir()
    bob_home = tmp_path / "bob"
    bob_home.mkdir()

    monkeypatch.setenv("COWORK_HOME", str(alice_home))
    alice_app = CoworkApp()

    async with alice_app.run_test() as alice_pilot:
        await _submit(alice_pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(alice_app.projects), timeout=5.0)
        alice_state = next(iter(alice_app.projects.values()))
        await _wait_for(lambda: bool(alice_state.channels), timeout=5.0)
        # Mint an invite URL via /invite.
        await _submit(alice_pilot, "/invite")
        # The /invite output puts the URL into the transcript.
        await _wait_for(
            lambda: "cowork://" in _transcript_text(alice_app), timeout=5.0
        )
        text = _transcript_text(alice_app)
        # Pick the LAST cowork:// occurrence — that's the freshly minted one.
        invite_url = text.split("cowork://")[-1].split()[0]
        invite_url = "cowork://" + invite_url

        # Bob's TUI starts in a different home and uses the URL as a single arg.
        monkeypatch.setenv("COWORK_HOME", str(bob_home))
        bob_app = CoworkApp()
        async with bob_app.run_test() as bob_pilot:
            await _submit(bob_pilot, f"/join {invite_url} bob")
            await _wait_for(lambda: bool(bob_app.projects), timeout=5.0)
            bob_state = next(iter(bob_app.projects.values()))
            await _wait_for(lambda: bool(bob_state.channels), timeout=5.0)

            # Bob posts; Alice should see the message.
            await _submit(bob_pilot, "hi alice")
            await _wait_for(
                lambda: any(
                    m["content"] == "hi alice"
                    for msgs in alice_state.messages_by_channel.values()
                    for m in msgs
                ),
                timeout=5.0,
            )
            # And vice versa.
            await _submit(alice_pilot, "hi bob")
            await _wait_for(
                lambda: any(
                    m["content"] == "hi bob"
                    for msgs in bob_state.messages_by_channel.values()
                    for m in msgs
                ),
                timeout=5.0,
            )


async def test_help_command_shows_command_reference() -> None:
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, "/help")
        text = _transcript_text(app)
        assert "/new-project" in text
        assert "/join" in text
        assert "ctrl+q" in text  # the new quit binding is documented


async def test_invalid_command_does_not_crash() -> None:
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, "/no-such-command")
        await pilot.pause()
        assert "unknown command" in _transcript_text(app)
        # App still responsive.
        assert app.is_running


async def test_channel_command_switches_channels(server: str) -> None:
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        state_fn = lambda: next(iter(app.projects.values()), None)
        await _wait_for(lambda: state_fn() and state_fn().channels, timeout=5.0)
        await _submit(pilot, "/channel new random")
        await _wait_for(
            lambda: any(c["name"] == "random" for c in state_fn().channels.values()),
            timeout=5.0,
        )
        await _submit(pilot, "/channel random")
        await pilot.pause()
        cur = state_fn().channels[app.current_channel_id]
        assert cur["name"] == "random"


async def test_save_transcript_writes_a_file(server: str, tmp_path: Path) -> None:
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        state_fn = lambda: next(iter(app.projects.values()), None)
        await _wait_for(lambda: state_fn() and state_fn().channels, timeout=5.0)
        await _submit(pilot, "first message")
        await _wait_for(
            lambda: any(
                m["content"] == "first message"
                for msgs in state_fn().messages_by_channel.values()
                for m in msgs
            ),
            timeout=5.0,
        )
        out_path = tmp_path / "out.txt"
        await _submit(pilot, f"/save-transcript {out_path}")
        await _wait_for(lambda: out_path.exists(), timeout=2.0)
        body = out_path.read_text()
        assert "first message" in body
        assert "@alice" in body


async def test_ctrl_c_does_not_quit_so_terminal_can_copy_selection() -> None:
    """Regression: ctrl+c used to be bound with priority=True, which hijacked
    Textual's selection-copy keystroke. Now ctrl+q is the quit binding."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        # Give the event loop a tick to process if anything was going to fire.
        await pilot.pause()
        assert app.is_running, "ctrl+c must not quit the app"


async def test_ctrl_q_quits_the_app() -> None:
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()
        # Quit may take a frame to propagate; the action_quit returns
        # quickly though.
        assert not app.is_running


async def test_richlog_allows_text_selection() -> None:
    """The transcript widget must permit text selection so users can drag-select
    and copy with their terminal's normal copy keystroke."""
    app = CoworkApp()
    async with app.run_test():
        log = app.query_one("#transcript", RichLog)
        assert log.allow_select is True


async def test_legacy_two_arg_join_still_works(server: str, tmp_path: Path) -> None:
    """Don't break the old `/join <server-url> <token> <name>` shape; people
    may have already shared invites in that form before the cowork:// URL."""
    # First make a project so we have a real token.
    import httpx

    async with httpx.AsyncClient(base_url=server) as http:
        r = await http.post(
            "/projects",
            json={"name": "demo", "creator_display_name": "creator"},
        )
        token = r.json()["default_invite_token"]
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/join {server} {token} bob")
        await _wait_for(lambda: bool(app.projects), timeout=5.0)
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels), timeout=5.0)


async def test_typed_text_reaches_input_widget_immediately() -> None:
    """Without the focus fix, keystrokes typed at app start would be eaten by
    the Tree widget instead of populating the Input."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Press a few letters; they should land in the Input.
        for ch in "hello":
            await pilot.press(ch)
        await pilot.pause()
        inp = app.query_one("#input", Input)
        assert inp.value == "hello", f"expected 'hello', got {inp.value!r}"
