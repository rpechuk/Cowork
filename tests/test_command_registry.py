"""Lock in the invariant that the slash-command registry is the single
source of truth — autocomplete, the dispatcher, and the registered
methods stay in sync because they all derive from COMMAND_REGISTRY.

If a contributor adds an `@command(...)` decorator and forgets the
method body (or vice versa), these tests fail loudly so the drift
gets caught at PR time instead of at runtime."""

from __future__ import annotations

import pytest

from cowork.client.tui import (
    COMMAND_REGISTRY,
    CoworkApp,
    command,
    slash_commands,
)
from textual.widgets import Input


# Every command we currently ship. If you add a new one, add it here too
# — the explicit list is a guard against accidentally registering
# commands the project doesn't intend to support.
EXPECTED_COMMANDS: set[str] = {
    "help",
    "quit",
    "new-project",
    "join",
    "channel",
    "invite",
    "leave-project",
    "status",
    "agent",
    "login",
    "save-transcript",
}


def test_command_registry_covers_every_expected_command() -> None:
    """Spot-check the set of registered commands matches what we ship.
    Catches both 'forgot to register' and 'registered a typo'."""
    registered = set(COMMAND_REGISTRY)
    missing = EXPECTED_COMMANDS - registered
    extra = registered - EXPECTED_COMMANDS
    assert not missing, f"commands missing from registry: {sorted(missing)}"
    assert not extra, (
        f"unexpected commands in registry: {sorted(extra)}"
        " (if intentional, add them to EXPECTED_COMMANDS in this test)"
    )


def test_every_registered_command_has_a_method_on_coworkapp() -> None:
    """The decorator stores the method name as a string — verify the
    actual method exists on CoworkApp. Without this check a typo in
    the decorator (`@command("agnt", ...)`) would only blow up when
    the user types the command."""
    for name, entry in COMMAND_REGISTRY.items():
        method_name = entry["method"]
        assert hasattr(CoworkApp, method_name), (
            f"command /{name} is registered to {method_name!r} but"
            f" CoworkApp has no such method"
        )
        assert callable(getattr(CoworkApp, method_name)), (
            f"CoworkApp.{method_name} is not callable"
        )


def test_slash_commands_includes_every_registered_command() -> None:
    """The autocomplete popup reads from `slash_commands()` — every
    registered command MUST appear there or it's effectively hidden."""
    listed = {name for name, _ in slash_commands()}
    assert listed == set(COMMAND_REGISTRY), (
        f"slash_commands() drift from COMMAND_REGISTRY: "
        f"missing={set(COMMAND_REGISTRY) - listed}, "
        f"extra={listed - set(COMMAND_REGISTRY)}"
    )


def test_command_decorator_registers_new_commands_in_place() -> None:
    """Sanity: registering a fresh decorator immediately mutates the
    registry. This is the 'auto-add' behavior we promised — no
    secondary list to update."""
    sentinel_name = "__test_sentinel_command__"
    assert sentinel_name not in COMMAND_REGISTRY
    try:

        @command(sentinel_name, "a sentinel for tests")
        async def _cmd_sentinel(self, args):
            return None

        assert sentinel_name in COMMAND_REGISTRY
        assert COMMAND_REGISTRY[sentinel_name]["description"] == (
            "a sentinel for tests"
        )
        # The decorator returns the original function unchanged so the
        # registered name maps to a working callable.
        assert COMMAND_REGISTRY[sentinel_name]["method"] == "_cmd_sentinel"
    finally:
        # Clean up so we don't pollute later tests in the same process.
        COMMAND_REGISTRY.pop(sentinel_name, None)


async def test_dispatcher_routes_via_registry_not_hardcoded_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatcher looks the handler up by name out of the registry.
    Patching the registry entry to point at a tracer method is the
    cleanest way to prove it — no hardcoded `elif cmd == "..."` to
    bypass us."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        seen: list[list[str]] = []

        async def tracer(self, args):
            seen.append(args)

        # Stash the real handler then redirect /status to the tracer.
        monkeypatch.setattr(CoworkApp, "_cmd_status_tracer", tracer, raising=False)
        original = COMMAND_REGISTRY["status"]["method"]
        COMMAND_REGISTRY["status"]["method"] = "_cmd_status_tracer"
        try:
            inp = app.query_one("#input", Input)
            inp.value = "/status here we go"
            inp.cursor_position = len(inp.value)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        finally:
            COMMAND_REGISTRY["status"]["method"] = original
        assert seen == [["here", "we", "go"]]


async def test_dispatcher_reports_unknown_command_clearly() -> None:
    """Unknown commands surface a helpful error pointing at /help."""
    app = CoworkApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#input", Input)
        inp.value = "/totally-bogus"
        inp.cursor_position = len(inp.value)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        from textual.widgets import RichLog
        log = app.query_one("#transcript", RichLog)
        text = "\n".join(
            getattr(line, "text", str(line)) for line in log.lines
        )
        assert "unknown command" in text
        assert "/help" in text
