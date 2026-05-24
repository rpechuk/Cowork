"""Live end-to-end smoke against a real `cowork serve` subprocess.

Spins up the real CLI server, launches two CoworkApp instances headlessly
via Textual's Pilot, and walks through the documented "first-time user"
flow:

    1. Alice runs the TUI, types /new-project against the live server.
    2. Alice runs /invite — the TUI prints a cowork://...#TOKEN URL.
    3. Bob runs the TUI (separate cache dir) and pastes /join <that URL>.
    4. Both exchange a message; each sees the other's text.

Anything that breaks here will break for a real user, so this is the
test we run when we want to be 100% sure connection + joining works.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from textual.widgets import Input, RichLog

from cowork.client.tui import CoworkApp


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def wait_http_alive(url: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(timeout=0.5) as client:
                # /openapi.json is FastAPI's default, exists once startup done.
                r = await client.get(f"{url}/openapi.json")
                if r.status_code == 200:
                    return
        except Exception:
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError(f"server at {url} did not come up in {timeout}s")


def transcript_text(app: CoworkApp) -> str:
    log = app.query_one("#transcript", RichLog)
    out = []
    for line in log.lines:
        try:
            out.append(line.text)
        except AttributeError:
            out.append(str(line))
    return "\n".join(out)


async def submit(pilot, text: str) -> None:
    inp = pilot.app.query_one("#input", Input)
    inp.value = text
    await pilot.press("enter")
    await pilot.pause()


async def wait_for(name: str, fn, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timeout waiting for: {name}")


async def main() -> int:
    workdir = tempfile.mkdtemp(prefix="cowork-live-")
    server_home = Path(workdir) / "server"
    server_home.mkdir()
    alice_home = Path(workdir) / "alice"
    alice_home.mkdir()
    bob_home = Path(workdir) / "bob"
    bob_home.mkdir()

    port = free_port()
    base = f"http://127.0.0.1:{port}"

    print(f"[1/6] starting `cowork serve` on port {port} ...")
    server_env = dict(os.environ)
    server_env["COWORK_HOME"] = str(server_home)
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "cowork.cli", "serve",
         "--host", "127.0.0.1", "--port", str(port)],
        env=server_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        await wait_http_alive(base, timeout=15.0)
        print(f"      server is live: {base}")

        print("[2/6] launching Alice's TUI; typing /new-project ...")
        os.environ["COWORK_HOME"] = str(alice_home)
        alice = CoworkApp()
        async with alice.run_test() as alice_pilot:
            # Critical regression test: input must already have focus.
            await alice_pilot.pause()
            assert alice.focused and alice.focused.id == "input", (
                f"Alice's input did NOT get focus — focused widget is {alice.focused}"
            )
            print("      ✓ Alice's input has focus on mount")

            await submit(alice_pilot, f"/new-project demo {base} alice")
            await wait_for("Alice has a project", lambda: bool(alice.projects))
            alice_state = next(iter(alice.projects.values()))
            await wait_for(
                "Alice has channels",
                lambda: bool(alice_state.channels),
            )
            print("      ✓ project created, WS connected, #general present")

            text = transcript_text(alice)
            assert "cowork://" in text, "no cowork:// URL in Alice's transcript"
            # Extract a fresh invite via /invite to be sure we exercise it.
            await submit(alice_pilot, "/invite")
            await wait_for(
                "/invite output present",
                lambda: transcript_text(alice).count("cowork://") >= 2,
            )
            invite_match = re.search(
                r"cowork[a-z+]*://[^\s]+#[A-Za-z0-9_\-]+",
                transcript_text(alice).split("/invite", 1)[-1] if "/invite" in transcript_text(alice) else transcript_text(alice),
            )
            # Fall back: just grab the last cowork:// URL in the buffer.
            if not invite_match:
                invite_match = re.search(
                    r"cowork[a-z+]*://[^\s]+#[A-Za-z0-9_\-]+",
                    transcript_text(alice),
                )
            assert invite_match, "couldn't extract a cowork:// URL"
            invite_url = invite_match.group(0)
            print(f"      ✓ minted invite URL: {invite_url}")

            print("[3/6] launching Bob's TUI in a separate home; /join <url> bob ...")
            os.environ["COWORK_HOME"] = str(bob_home)
            bob = CoworkApp()
            async with bob.run_test() as bob_pilot:
                await bob_pilot.pause()
                assert bob.focused and bob.focused.id == "input"

                await submit(bob_pilot, f"/join {invite_url} bob")
                await wait_for("Bob has a project", lambda: bool(bob.projects))
                bob_state = next(iter(bob.projects.values()))
                await wait_for("Bob has channels", lambda: bool(bob_state.channels))
                print("      ✓ Bob joined via single-arg cowork:// URL")

                print("[4/6] Alice posts a message; Bob receives it ...")
                await submit(alice_pilot, "hello bob from alice")
                await wait_for(
                    "Bob saw Alice's message",
                    lambda: any(
                        m["content"] == "hello bob from alice"
                        for msgs in bob_state.messages_by_channel.values()
                        for m in msgs
                    ),
                )
                print("      ✓ Bob received Alice's broadcast")

                print("[5/6] Bob replies; Alice receives it ...")
                await submit(bob_pilot, "hi alice, got your message")
                await wait_for(
                    "Alice saw Bob's reply",
                    lambda: any(
                        m["content"] == "hi alice, got your message"
                        for msgs in alice_state.messages_by_channel.values()
                        for m in msgs
                    ),
                )
                print("      ✓ Alice received Bob's reply")

                print("[6/6] checking mention bell + cross-channel notifications ...")
                await submit(bob_pilot, "/channel new random")
                await wait_for(
                    "random channel exists",
                    lambda: any(c["name"] == "random" for c in alice_state.channels.values()),
                )
                # Bob switches to #random so #general is unfocused for him.
                await submit(bob_pilot, "/channel random")
                await asyncio.sleep(0.2)
                await submit(alice_pilot, "@bob ping in general")
                await wait_for(
                    "Bob's unread for #general includes a mention",
                    lambda: any(
                        bob_state.unread.get(cid, (0, 0))[1] > 0
                        for cid in bob_state.channels
                        if bob_state.channels[cid]["name"] == "general"
                    ),
                )
                print("      ✓ Bob got a mention frame for an unfocused channel")

        print("\nALL CHECKS PASSED — connection + joining are working end-to-end.")
        return 0
    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
