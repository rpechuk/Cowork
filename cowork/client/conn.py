from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
import websockets

logger = logging.getLogger("cowork.client")

FrameHandler = Callable[[str, dict], Awaitable[None]]


def http_base(server_url: str) -> str:
    return server_url.rstrip("/")


def ws_url(server_url: str, token: str, project_id: str) -> str:
    parsed = urlparse(server_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = urlencode({"token": token, "project_id": project_id})
    return urlunparse((scheme, parsed.netloc, parsed.path + "/ws", "", query, ""))


class ServerError(RuntimeError):
    pass


async def http_create_project(server_url: str, name: str, display_name: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{http_base(server_url)}/projects",
            json={"name": name, "creator_display_name": display_name},
        )
    if r.status_code >= 400:
        raise ServerError(r.text)
    return r.json()


async def http_redeem_invite(server_url: str, invite_token: str, display_name: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{http_base(server_url)}/invites/redeem",
            json={"invite_token": invite_token, "display_name": display_name},
        )
    if r.status_code >= 400:
        raise ServerError(r.text)
    return r.json()


async def http_mint_invite(server_url: str, member_token: str, project_id: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{http_base(server_url)}/projects/{project_id}/invites",
            json={"max_uses": None, "expires_in_seconds": None},
            headers={"Authorization": f"Bearer {member_token}"},
        )
    if r.status_code >= 400:
        raise ServerError(r.text)
    return r.json()


async def http_bootstrap(server_url: str, member_token: str, project_id: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{http_base(server_url)}/projects/{project_id}/bootstrap",
            headers={"Authorization": f"Bearer {member_token}"},
        )
    if r.status_code >= 400:
        raise ServerError(r.text)
    return r.json()


async def http_create_agent(
    server_url: str,
    member_token: str,
    project_id: str,
    *,
    display_name: str,
    system_prompt: str = "",
    trigger_mode: str = "on_mention",
    model: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{http_base(server_url)}/projects/{project_id}/agents",
            json={
                "display_name": display_name,
                "system_prompt": system_prompt,
                "trigger_mode": trigger_mode,
                "model": model,
                "channel_id": channel_id,
            },
            headers={"Authorization": f"Bearer {member_token}"},
        )
    if r.status_code >= 400:
        raise ServerError(r.text)
    return r.json()


async def http_delete_agent(
    server_url: str, member_token: str, project_id: str, agent_id: str
) -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.delete(
            f"{http_base(server_url)}/projects/{project_id}/agents/{agent_id}",
            headers={"Authorization": f"Bearer {member_token}"},
        )
    if r.status_code >= 400:
        raise ServerError(r.text)


class ProjectConnection:
    """One WebSocket connection per joined project. Reconnects with backoff."""

    def __init__(
        self,
        server_url: str,
        project_id: str,
        member_token: str,
        on_frame: FrameHandler,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.server_url = server_url
        self.project_id = project_id
        self.member_token = member_token
        self.on_frame = on_frame
        self.on_status = on_status or (lambda s: None)
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._send_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            try:
                await self._task
            except Exception:
                pass

    async def send(self, frame: dict) -> None:
        await self._send_queue.put(frame)

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            url = ws_url(self.server_url, self.member_token, self.project_id)
            try:
                async with websockets.connect(url, max_size=2**22) as ws:
                    self._ws = ws
                    self.on_status("connected")
                    backoff = 1.0
                    pump = asyncio.create_task(self._pump_outbound(ws))
                    try:
                        async for raw in ws:
                            try:
                                frame = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            await self.on_frame(frame.get("type") or "", frame.get("data") or {})
                    finally:
                        pump.cancel()
                        try:
                            await pump
                        except (asyncio.CancelledError, Exception):
                            pass
                        self._ws = None
            except Exception as e:
                self.on_status(f"disconnected: {e}")
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    async def _pump_outbound(self, ws) -> None:
        while True:
            frame = await self._send_queue.get()
            try:
                await ws.send(json.dumps(frame))
            except Exception:
                # Put it back so a reconnect can retry.
                await self._send_queue.put(frame)
                return
