"""Coverage for the `/login` TUI command, which shells out to `claude
login` so the local Claude CLI / SDK is authenticated for agent calls.

The subprocess hook is overridden in every test — we don't want to
actually spawn the real CLI (it would pop a browser and block forever
on its OAuth prompts)."""

from __future__ import annotations

import asyncio

import pytest

from cowork.client.tui import CoworkApp
from textual.widgets import Input, RichLog


async def _submit(pilot, text: str) -> None:
    inp = pilot.app.query_one("#input", Input)
    inp.value = text
    inp.cursor_position = len(text)
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


def _transcript_text(app: CoworkApp) -> str:
    log = app.query_one("#transcript", RichLog)
    chunks: list[str] = []
    for line in log.lines:
        try:
            chunks.append(line.text)
        except Exception:
            chunks.append(str(line))
    return "\n".join(chunks)


async def test_login_command_warns_when_claude_cli_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the `claude` binary isn't on PATH we must tell the user how to
    install it — not silently appear to do nothing or hang."""
    # `shutil.which` is imported in cowork.client.tui at module load, so
    # we patch it on the module to short-circuit the lookup.
    import cowork.client.tui as tui_mod
    monkeypatch.setattr(tui_mod.shutil, "which", lambda name: None)

    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, "/login")
        text = _transcript_text(app)
        assert "not found" in text
        assert "claude-code" in text  # the install hint mentions the npm pkg


async def test_login_command_calls_subprocess_and_reports_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: `claude` exists, the subprocess returns 0, we surface a
    green success line. We replace `_run_claude_login` directly so we
    never actually shell out (which would block on a browser flow)."""
    import cowork.client.tui as tui_mod
    monkeypatch.setattr(
        tui_mod.shutil, "which", lambda name: f"/fake/bin/{name}"
    )

    calls: list[bool] = []

    async def fake_run(self):
        calls.append(True)
        return 0

    monkeypatch.setattr(CoworkApp, "_run_claude_login", fake_run)

    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, "/login")
        assert calls == [True], "expected exactly one subprocess invocation"
        text = _transcript_text(app)
        assert "Authenticated" in text


async def test_login_command_surfaces_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `claude login` exits non-zero we surface that to the user with
    the exit code, so they can either retry or run it manually."""
    import cowork.client.tui as tui_mod
    monkeypatch.setattr(
        tui_mod.shutil, "which", lambda name: f"/fake/bin/{name}"
    )

    async def fake_run(self):
        return 7

    monkeypatch.setattr(CoworkApp, "_run_claude_login", fake_run)

    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(pilot, "/login")
        text = _transcript_text(app)
        assert "exited with status 7" in text


async def test_login_appears_in_slash_autocomplete() -> None:
    """The autocomplete menu and HELP_TEXT both need to know about /login
    — otherwise users won't discover it."""
    from cowork.client.tui import COMMAND_REGISTRY, HELP_TEXT, slash_commands

    assert "login" in COMMAND_REGISTRY
    names = {name for name, _ in slash_commands()}
    assert "login" in names
    assert "/login" in HELP_TEXT
