"""Shared fixtures and helpers for the Cowork test suite.

Each test gets a freshly started in-process uvicorn server backed by a temporary
SQLite database. The `client` fixture wraps `httpx.AsyncClient` against that
server; `ws_for` opens an authenticated WebSocket for a given member.
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
import uvicorn


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest_asyncio.fixture
async def server(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """Start a fresh server in-process. Yields the base URL."""
    tmpdir = tempfile.mkdtemp(prefix="cowork-test-")
    monkeypatch.setenv("COWORK_HOME", tmpdir)

    from cowork.server.app import app

    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    uv = uvicorn.Server(config)
    task = asyncio.create_task(uv.serve())
    for _ in range(100):
        if uv.started:
            break
        await asyncio.sleep(0.05)
    assert uv.started, "server did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        uv.should_exit = True
        await task


@pytest_asyncio.fixture
async def client(server: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server, timeout=5.0) as c:
        yield c


# ---- helpers (imported by tests as needed) ----


def ws_url(base: str, token: str, project_id: str) -> str:
    return base.replace("http://", "ws://") + f"/ws?token={token}&project_id={project_id}"


async def recv_frame(ws, frame_type: str, *, timeout: float = 2.0) -> dict:
    """Receive frames until one matches `frame_type`. Raises on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(f"timeout waiting for frame type {frame_type!r}")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        frame = json.loads(raw)
        if frame.get("type") == frame_type:
            return frame


async def drain(ws, *, quiet_for: float = 0.1) -> list[dict]:
    """Drain frames until no new frame arrives for `quiet_for` seconds."""
    frames: list[dict] = []
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=quiet_for)
        except asyncio.TimeoutError:
            return frames
        frames.append(json.loads(raw))


async def assert_no_frame(ws, frame_type: str, *, within: float = 0.2) -> None:
    """Assert that no frame of `frame_type` arrives within the window."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            return
        frame = json.loads(raw)
        if frame.get("type") == frame_type:
            raise AssertionError(f"unexpected frame received: {frame}")


async def send(ws, frame_type: str, **data) -> None:
    await ws.send(json.dumps({"type": frame_type, "data": data}))


# ---- high-level helpers for scenario tests ----


async def create_project(
    client: httpx.AsyncClient, name: str, display_name: str
) -> dict:
    r = await client.post(
        "/projects",
        json={"name": name, "creator_display_name": display_name},
    )
    r.raise_for_status()
    return r.json()


async def redeem_invite(
    client: httpx.AsyncClient, invite_token: str, display_name: str
) -> dict:
    r = await client.post(
        "/invites/redeem",
        json={"invite_token": invite_token, "display_name": display_name},
    )
    r.raise_for_status()
    return r.json()


async def bootstrap(
    client: httpx.AsyncClient, member_token: str, project_id: str
) -> dict:
    r = await client.get(
        f"/projects/{project_id}/bootstrap",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    r.raise_for_status()
    return r.json()
