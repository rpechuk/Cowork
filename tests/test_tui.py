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
    """Type `text` into the input box and press Enter, the way a user would.
    Mirrors real typing by parking the cursor at end-of-value (otherwise
    the autocomplete sees an empty partial at position 0 and pops a menu
    that swallows the Enter press)."""
    inp = pilot.app.query_one("#input", Input)
    inp.value = text
    inp.cursor_position = len(text)
    # Bare value-set in Textual fires Input.Changed asynchronously; settle
    # before pressing enter so the autocomplete refresh sees the real value.
    await pilot.pause()
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


async def test_ctrl_c_quits_the_app() -> None:
    """ctrl+c quits — matches the universal terminal convention. The user
    relies on the terminal's own copy keystroke (cmd+c / ctrl+shift+c) for
    selecting text from the transcript, so we don't reserve ctrl+c for copy."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not app.is_running, "ctrl+c must quit the app"


async def test_ctrl_s_toggles_terminal_mouse_capture() -> None:
    """By default Cowork starts in selection-friendly mode: drag tracking
    (xterm modes 1002/1003) is disabled so native terminal text selection
    works out of the box. ctrl+s toggles to full TUI mouse mode (drag
    tracking on, orange status bar). Pressing it again restores the default."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Default state: selection-friendly (drag tracking off).
        assert app._mouse_capture_on is False
        status = app.query_one("#status")
        assert "tui-mouse-mode" not in status.classes

        # First ctrl+s: enable full TUI mouse mode.
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app._mouse_capture_on is True
        assert "tui-mouse-mode" in status.classes

        # Second ctrl+s: back to selection-friendly mode.
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app._mouse_capture_on is False
        assert "tui-mouse-mode" not in status.classes


async def test_real_textual_driver_exposes_mouse_escape_write() -> None:
    """Compile-time check that the LinuxDriver still exposes write/flush so
    our escape-sequence approach works on a real terminal."""
    from textual.drivers.linux_driver import LinuxDriver

    assert hasattr(LinuxDriver, "write")
    assert hasattr(LinuxDriver, "flush")


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


async def test_ctrl_i_returns_focus_to_input() -> None:
    """ctrl+i is the one keyboard nav we keep — snap back to the input
    from wherever focus has drifted (e.g. after clicking into the tree)."""
    from textual.widgets import Tree

    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#proj-tree", Tree).focus()
        await pilot.pause()
        assert isinstance(app.focused, Tree)
        await pilot.press("ctrl+i")
        await pilot.pause()
        assert app.focused is not None and app.focused.id == "input"


async def test_input_history_walks_back_with_up_arrow() -> None:
    """↑ cycles back through previously-submitted lines; ↓ walks forward;
    going past the newest entry restores whatever the user had been
    drafting before they started navigating."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Populate history with three submissions (any non-empty text the
        # input would accept; we don't need them to actually reach a server
        # for this test — Input.Submitted is the only thing that records
        # history).
        for line in ("first", "second", "third"):
            await _submit(pilot, line)
            await pilot.pause()
        inp = app.query_one("#input", Input)
        assert app._input_history == ["first", "second", "third"]
        # User starts drafting a new message.
        inp.value = "drafty"
        inp.cursor_position = len("drafty")

        await pilot.press("up")
        await pilot.pause()
        assert inp.value == "third"
        await pilot.press("up")
        await pilot.pause()
        assert inp.value == "second"
        await pilot.press("up")
        await pilot.pause()
        assert inp.value == "first"
        # Further ↑ at the oldest entry is a no-op.
        await pilot.press("up")
        await pilot.pause()
        assert inp.value == "first"

        # ↓ walks forward.
        await pilot.press("down")
        await pilot.pause()
        assert inp.value == "second"
        await pilot.press("down")
        await pilot.pause()
        assert inp.value == "third"
        # Past the newest, the stashed draft is restored.
        await pilot.press("down")
        await pilot.pause()
        assert inp.value == "drafty"
        assert app._history_idx is None


async def test_input_history_dedupes_repeated_submissions() -> None:
    """Submitting the same line twice in a row should only add one entry
    to the history list — otherwise ↑ ↑ ↑ would just bounce on the same
    line."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        for line in ("hi", "hi", "hi", "bye"):
            await _submit(pilot, line)
            await pilot.pause()
        assert app._input_history == ["hi", "bye"]


async def test_arrow_keys_drive_autocomplete_before_history() -> None:
    """When the autocomplete popup is open, ↑/↓ navigate it. Only with
    the popup closed do the arrow keys walk the input history."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Seed history so ↑ has somewhere to go.
        await _submit(pilot, "remember me")
        await pilot.pause()
        # Open the slash popup.
        inp = app.query_one("#input", Input)
        inp.value = "/"
        inp.cursor_position = 1
        app._refresh_autocomplete()
        await pilot.pause()
        assert app._ac_anchor is not None
        # ↑ should NOT load 'remember me' — it moves the popup selection.
        await pilot.press("up")
        await pilot.pause()
        assert inp.value == "/", inp.value
        # Close the popup, then ↑ should load history.
        await pilot.press("escape")
        await pilot.pause()
        inp.value = ""
        inp.cursor_position = 0
        await pilot.press("up")
        await pilot.pause()
        assert inp.value == "remember me"


# ---------------------------------------------------------------------------
# /status command + member-status-changed propagation
# ---------------------------------------------------------------------------


async def test_status_command_updates_local_state_and_other_clients(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When alice runs /status away, bob (in the same project) sees alice's
    status update reflected in his members panel. End-to-end through the
    real server, real WS broadcast, real DB."""
    alice_home = tmp_path / "alice"
    alice_home.mkdir()
    bob_home = tmp_path / "bob"
    bob_home.mkdir()

    monkeypatch.setenv("COWORK_HOME", str(alice_home))
    alice = CoworkApp()
    async with alice.run_test() as alice_pilot:
        await _submit(alice_pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(alice.projects), timeout=5.0)
        a_state = next(iter(alice.projects.values()))
        await _wait_for(lambda: bool(a_state.channels), timeout=5.0)
        await _submit(alice_pilot, "/invite")
        await _wait_for(
            lambda: "cowork://" in _transcript_text(alice), timeout=5.0
        )
        invite_url = "cowork://" + _transcript_text(alice).split("cowork://")[-1].split()[0]

        monkeypatch.setenv("COWORK_HOME", str(bob_home))
        bob = CoworkApp()
        async with bob.run_test() as bob_pilot:
            await _submit(bob_pilot, f"/join {invite_url} bob")
            await _wait_for(lambda: bool(bob.projects), timeout=5.0)
            b_state = next(iter(bob.projects.values()))
            await _wait_for(lambda: len(b_state.members) == 2, timeout=5.0)

            # alice flips to /status busy. bob should see it.
            await _submit(alice_pilot, "/status busy")
            await _wait_for(
                lambda: any(
                    m["display_name"] == "alice" and m.get("status") == "busy"
                    for m in b_state.members.values()
                ),
                timeout=5.0,
            )
            # alice's own state should also reflect it.
            await _wait_for(
                lambda: a_state.members.get(a_state.cached.member_id, {}).get("status")
                == "busy",
                timeout=5.0,
            )


async def test_auto_status_flips_to_away_when_idle(server: str) -> None:
    """When keyboard activity stops for longer than the idle threshold the
    watchdog flips the user from online → away on the server, which
    broadcasts the change back to our own client. The very next keystroke
    flips it back to online."""
    app = CoworkApp()
    # Tighten the watchdog so the test doesn't have to wait two minutes.
    app._idle_threshold_s = 0.2
    app._idle_check_interval_s = 0.05
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects), timeout=5.0)
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels), timeout=5.0)
        my_id = state.cached.member_id
        # Wait for the watchdog to notice idleness and flip us to 'away'.
        # (Project setup itself takes long enough to exceed the 0.2s
        # threshold, so this should happen within a tick or two.)
        await _wait_for(
            lambda: state.members.get(my_id, {}).get("status") == "away",
            timeout=5.0,
        )
        # Pin the threshold high enough that the watchdog won't immediately
        # re-flip us to away after the snap-back — otherwise the test races
        # against itself in the ~0.2s window before idle exceeds threshold.
        app._idle_threshold_s = 60.0
        await pilot.press("a")
        await _wait_for(
            lambda: state.members.get(my_id, {}).get("status") == "online",
            timeout=5.0,
        )


async def test_manual_status_pins_and_blocks_auto_update(server: str) -> None:
    """After /status busy, the idle watchdog must NOT silently flip the
    user to away. Manual presence wins."""
    app = CoworkApp()
    app._idle_threshold_s = 0.2
    app._idle_check_interval_s = 0.05
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects), timeout=5.0)
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels), timeout=5.0)
        await _submit(pilot, "/status busy")
        await _wait_for(
            lambda: state.members.get(state.cached.member_id, {}).get("status")
            == "busy",
            timeout=5.0,
        )
        # Sit still well past the idle threshold; status should NOT change.
        await asyncio.sleep(0.6)
        cur = state.members.get(state.cached.member_id, {}).get("status")
        assert cur == "busy", f"manual /status busy got overridden to {cur!r}"
        assert app._user_set_status is True


async def test_member_goes_offline_when_their_session_closes(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When alice closes her client, bob (still connected to the same
    project) should see her flip to 'offline' on the members panel.
    When she relaunches the app, the server re-broadcasts her stored
    status preference and bob sees her come back online."""
    alice_home = tmp_path / "alice"
    alice_home.mkdir()
    bob_home = tmp_path / "bob"
    bob_home.mkdir()

    # 1) Alice creates the project and grabs an invite URL.
    monkeypatch.setenv("COWORK_HOME", str(alice_home))
    alice = CoworkApp()
    async with alice.run_test() as alice_pilot:
        await _submit(alice_pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(alice.projects), timeout=5.0)
        await _submit(alice_pilot, "/invite")
        await _wait_for(
            lambda: "cowork://" in _transcript_text(alice), timeout=5.0
        )
        invite_url = (
            "cowork://" + _transcript_text(alice).split("cowork://")[-1].split()[0]
        )

    # 2) Bob joins. With alice's app exited, the bootstrap should show her
    # as 'offline' immediately — no waiting for a disconnect event because
    # her WS was already gone before bob even logged in.
    monkeypatch.setenv("COWORK_HOME", str(bob_home))
    bob = CoworkApp()
    async with bob.run_test() as bob_pilot:
        await _submit(bob_pilot, f"/join {invite_url} bob")
        await _wait_for(lambda: bool(bob.projects), timeout=5.0)
        b_state = next(iter(bob.projects.values()))
        await _wait_for(lambda: len(b_state.members) == 2, timeout=5.0)

        alice_member = next(
            m for m in b_state.members.values() if m["display_name"] == "alice"
        )
        alice_id = alice_member["id"]
        await _wait_for(
            lambda: b_state.members.get(alice_id, {}).get("status") == "offline",
            timeout=5.0,
        )

        # 3) Alice relaunches her app. Bob should see her come back to
        # online without bob having to reload — the server emits a
        # member_status_changed broadcast on her first WS reconnect.
        monkeypatch.setenv("COWORK_HOME", str(alice_home))
        alice2 = CoworkApp()
        async with alice2.run_test() as _:
            await _wait_for(lambda: bool(alice2.projects), timeout=5.0)
            await _wait_for(
                lambda: b_state.members.get(alice_id, {}).get("status")
                != "offline",
                timeout=5.0,
            )

        # 4) Once alice2 exits, bob should see her flip back to 'offline'
        # — confirms the disconnect path emits the broadcast too, not just
        # bootstrap.
        await _wait_for(
            lambda: b_state.members.get(alice_id, {}).get("status") == "offline",
            timeout=5.0,
        )


async def test_bootstrap_reports_disconnected_members_as_offline(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If alice has left the building and bob *then* logs in for the first
    time, bob's bootstrap should already show alice as offline — the
    server overlays the presence on top of her stored status preference."""
    alice_home = tmp_path / "alice"
    alice_home.mkdir()
    bob_home = tmp_path / "bob"
    bob_home.mkdir()

    monkeypatch.setenv("COWORK_HOME", str(alice_home))
    alice = CoworkApp()
    invite_url: str
    async with alice.run_test() as alice_pilot:
        await _submit(alice_pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(alice.projects), timeout=5.0)
        await _submit(alice_pilot, "/invite")
        await _wait_for(
            lambda: "cowork://" in _transcript_text(alice), timeout=5.0
        )
        invite_url = (
            "cowork://" + _transcript_text(alice).split("cowork://")[-1].split()[0]
        )
    # alice's `async with` exited → her connection has been torn down.

    monkeypatch.setenv("COWORK_HOME", str(bob_home))
    bob = CoworkApp()
    async with bob.run_test() as bob_pilot:
        await _submit(bob_pilot, f"/join {invite_url} bob")
        await _wait_for(lambda: bool(bob.projects), timeout=5.0)
        b_state = next(iter(bob.projects.values()))
        await _wait_for(lambda: len(b_state.members) == 2, timeout=5.0)
        alice_member = next(
            m for m in b_state.members.values() if m["display_name"] == "alice"
        )
        assert alice_member["status"] == "offline", alice_member


async def test_status_command_rejects_unknown_preset(server: str) -> None:
    """The protocol enforces fixed presets server-side. Garbage doesn't get
    accepted and the client surfaces an error."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects), timeout=5.0)
        await _submit(pilot, "/status not-a-real-status")
        # Local validation rejects it before it ever hits the wire.
        assert "unknown status" in _transcript_text(app)


# ---------------------------------------------------------------------------
# @-mention autocomplete
# ---------------------------------------------------------------------------


async def test_at_autocomplete_appears_when_typing_at(server: str) -> None:
    """Type '@' in the input → the autocomplete popup opens with member names."""
    from textual.widgets import OptionList

    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects), timeout=5.0)
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels), timeout=5.0)

        inp = app.query_one("#input", Input)
        inp.value = "hello @"
        inp.cursor_position = len("hello @")
        # Input.Changed fires off cursor moves/value sets only when value
        # actually changes via user input; trigger refresh manually.
        app._refresh_autocomplete()
        await pilot.pause()

        ac = app.query_one("#autocomplete", OptionList)
        assert "visible" in ac.classes
        # @here and @channel are always offered as broadcast tokens.
        names = [opt.id for opt in [ac.get_option_at_index(i) for i in range(ac.option_count)]]
        assert "here" in names
        assert "channel" in names


async def test_at_autocomplete_tab_inserts_selected_name(server: str) -> None:
    """Tab accepts the highlighted option, replacing the partial @<text>
    with the full @<name> + trailing space."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects), timeout=5.0)
        state = next(iter(app.projects.values()))
        await _wait_for(lambda: bool(state.channels), timeout=5.0)

        inp = app.query_one("#input", Input)
        # No other humans in the project yet, but @here is always offered.
        inp.value = "hey @he"
        inp.cursor_position = len("hey @he")
        app._refresh_autocomplete()
        await pilot.pause()

        # Tab accepts the first match.
        await pilot.press("tab")
        await pilot.pause()
        assert inp.value == "hey @here ", inp.value
        # Popup is now hidden.
        assert app._ac_anchor is None


async def test_at_autocomplete_escape_dismisses(server: str) -> None:
    """Escape closes the popup without modifying the input value."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects), timeout=5.0)
        await _wait_for(
            lambda: bool(next(iter(app.projects.values())).channels), timeout=5.0
        )
        inp = app.query_one("#input", Input)
        inp.value = "@he"
        inp.cursor_position = 3
        app._refresh_autocomplete()
        await pilot.pause()
        assert app._ac_anchor is not None
        await pilot.press("escape")
        await pilot.pause()
        assert app._ac_anchor is None
        # Input value unchanged.
        assert inp.value == "@he"


async def test_slash_autocomplete_lists_commands(server: str) -> None:
    """Typing '/' at the start of input opens the autocomplete with the
    full command list; typing more characters filters it down."""
    from textual.widgets import OptionList

    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#input", Input)
        inp.value = "/"
        inp.cursor_position = 1
        app._refresh_autocomplete()
        await pilot.pause()

        ac = app.query_one("#autocomplete", OptionList)
        assert "visible" in ac.classes
        assert app._ac_mode == "slash"
        ids = [ac.get_option_at_index(i).id for i in range(ac.option_count)]
        assert "help" in ids and "status" in ids and "quit" in ids

        # Filter to commands starting with 's'.
        inp.value = "/s"
        inp.cursor_position = 2
        app._refresh_autocomplete()
        await pilot.pause()
        ids = [ac.get_option_at_index(i).id for i in range(ac.option_count)]
        assert ids == ["status", "save-transcript"]


async def test_slash_autocomplete_tab_inserts_command(server: str) -> None:
    """Tab on the slash popup replaces the partial command with the full
    one plus a trailing space, ready for the user to keep typing args."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#input", Input)
        inp.value = "/sta"
        inp.cursor_position = 4
        app._refresh_autocomplete()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert inp.value == "/status ", inp.value
        assert app._ac_anchor is None


async def test_slash_autocomplete_hides_after_space(server: str) -> None:
    """Once the user types past the command name (with a space), the popup
    closes — they're now typing arguments, not picking a command."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#input", Input)
        inp.value = "/status "
        inp.cursor_position = len(inp.value)
        app._refresh_autocomplete()
        await pilot.pause()
        assert app._ac_anchor is None


async def test_slash_autocomplete_ignores_mid_message_slash() -> None:
    """'/' is only a sigil at the start of input — 'do /it' must not pop."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#input", Input)
        inp.value = "do /it"
        inp.cursor_position = len(inp.value)
        app._refresh_autocomplete()
        await pilot.pause()
        assert app._ac_anchor is None


async def test_at_autocomplete_does_not_trigger_on_email(server: str) -> None:
    """`alice@here.com` must NOT pop the autocomplete — the @ has to start a
    word, matching the server's MENTION_RE rule."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await _submit(pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(app.projects), timeout=5.0)
        await _wait_for(
            lambda: bool(next(iter(app.projects.values())).channels), timeout=5.0
        )
        inp = app.query_one("#input", Input)
        inp.value = "ping alice@he"
        inp.cursor_position = len(inp.value)
        app._refresh_autocomplete()
        await pilot.pause()
        assert app._ac_anchor is None


# ---------------------------------------------------------------------------
# Recent-mentions feed
# ---------------------------------------------------------------------------


async def test_mentions_feed_records_recent_mentions(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When bob @-mentions alice in a channel alice isn't viewing, the
    mention shows up in the recent-@mentions feed on alice's left sidebar."""
    alice_home = tmp_path / "alice"
    alice_home.mkdir()
    bob_home = tmp_path / "bob"
    bob_home.mkdir()

    monkeypatch.setenv("COWORK_HOME", str(alice_home))
    alice = CoworkApp()
    async with alice.run_test() as alice_pilot:
        await _submit(alice_pilot, f"/new-project demo {server} alice")
        await _wait_for(lambda: bool(alice.projects), timeout=5.0)
        a_state = next(iter(alice.projects.values()))
        await _wait_for(lambda: bool(a_state.channels), timeout=5.0)
        # Create a second channel so alice can have something to NOT be focused on.
        await _submit(alice_pilot, "/channel new random")
        await _wait_for(
            lambda: any(c["name"] == "random" for c in a_state.channels.values()),
            timeout=5.0,
        )
        # alice switches to #random so #general is unfocused.
        await _submit(alice_pilot, "/channel random")
        await pilot_pause_brief()

        await _submit(alice_pilot, "/invite")
        await _wait_for(
            lambda: "cowork://" in _transcript_text(alice), timeout=5.0
        )
        invite_url = "cowork://" + _transcript_text(alice).split("cowork://")[-1].split()[0]

        monkeypatch.setenv("COWORK_HOME", str(bob_home))
        bob = CoworkApp()
        async with bob.run_test() as bob_pilot:
            await _submit(bob_pilot, f"/join {invite_url} bob")
            await _wait_for(lambda: bool(bob.projects), timeout=5.0)
            b_state = next(iter(bob.projects.values()))
            await _wait_for(lambda: bool(b_state.channels), timeout=5.0)
            # bob posts @alice in #general; alice is NOT focused there → mention.
            await _submit(bob_pilot, "@alice are you around?")
            await _wait_for(
                lambda: bool(a_state.recent_mentions),
                timeout=5.0,
            )
            entry = a_state.recent_mentions[0]
            assert entry["by_display_name"] == "bob"
            assert "are you around" in entry["preview"]


async def pilot_pause_brief() -> None:
    """Short asyncio sleep — wrapper around what tests need when waiting for
    a WS round-trip that has no observable predicate yet."""
    await asyncio.sleep(0.15)


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


async def test_channel_switch_updates_server_side_focus(server: str) -> None:
    """Regression: switching to an empty channel didn't notify the server, so
    mentions delivered to the *previous* channel after the switch were
    suppressed (server still thought we were focused there). Now /channel <x>
    always sends mark_read so focused_channel_id moves with the user."""
    import json as _json

    import httpx
    import websockets

    async with httpx.AsyncClient(base_url=server) as http:
        r = await http.post(
            "/projects",
            json={"name": "p", "creator_display_name": "alice"},
        )
        alice = r.json()
    bob_app = CoworkApp()
    async with bob_app.run_test() as bob_pilot:
        await bob_pilot.pause()
        await _submit(
            bob_pilot, f"/join {server} {alice['default_invite_token']} bob"
        )
        await _wait_for(lambda: bool(bob_app.projects), timeout=5.0)
        bob_state = next(iter(bob_app.projects.values()))
        await _wait_for(lambda: bool(bob_state.channels), timeout=5.0)
        general_id = next(
            cid for cid, ch in bob_state.channels.items() if ch["name"] == "general"
        )

        # Bob creates and switches to a fresh empty channel.
        await _submit(bob_pilot, "/channel new other")
        await _wait_for(
            lambda: any(c["name"] == "other" for c in bob_state.channels.values()),
            timeout=5.0,
        )
        await _submit(bob_pilot, "/channel other")
        await asyncio.sleep(0.3)  # let the focus-update WS frame round-trip

        # Alice (via raw WS) mentions Bob in #general — Bob is focused on
        # #other so he should receive a `mention` frame, not just a regular
        # message.
        ws_url_alice = (
            server.replace("http://", "ws://")
            + f"/ws?token={alice['member_token']}&project_id={alice['project_id']}"
        )
        async with websockets.connect(ws_url_alice) as ws_a:
            await ws_a.recv()  # drain hello
            await ws_a.send(
                _json.dumps(
                    {
                        "type": "send_message",
                        "data": {
                            "channel_id": general_id,
                            "content": "@bob wake up",
                        },
                    }
                )
            )
        await _wait_for(
            lambda: bob_state.unread.get(general_id, (0, 0))[1] >= 1,
            timeout=5.0,
        )


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
