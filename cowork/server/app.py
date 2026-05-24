from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)

from cowork.paths import server_db_path
from cowork.server.db import Database
from cowork.shared.protocol import (
    BootstrapResponse,
    CreateProjectRequest,
    CreateProjectResponse,
    MintInviteRequest,
    MintInviteResponse,
    RedeemInviteRequest,
    RedeemInviteResponse,
)

logger = logging.getLogger("cowork.server")


class ConnectionManager:
    """Tracks live WS connections per project so we can fan out events."""

    def __init__(self) -> None:
        self._by_project: dict[str, set["ClientSession"]] = {}
        self._lock = asyncio.Lock()

    async def add(self, sess: "ClientSession") -> None:
        async with self._lock:
            self._by_project.setdefault(sess.project_id, set()).add(sess)

    async def remove(self, sess: "ClientSession") -> None:
        async with self._lock:
            bucket = self._by_project.get(sess.project_id)
            if bucket:
                bucket.discard(sess)
                if not bucket:
                    self._by_project.pop(sess.project_id, None)

    def sessions_for(self, project_id: str) -> list["ClientSession"]:
        return list(self._by_project.get(project_id, ()))


class ClientSession:
    def __init__(self, ws: WebSocket, member_id: str, project_id: str) -> None:
        self.ws = ws
        self.member_id = member_id
        self.project_id = project_id
        self.focused_channel_id: Optional[str] = None

    async def send(self, payload: dict) -> None:
        try:
            await self.ws.send_text(json.dumps(payload))
        except Exception:
            logger.exception("failed to send frame")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(server_db_path())
    await db.connect()
    app.state.db = db
    app.state.conn_manager = ConnectionManager()
    try:
        yield
    finally:
        await db.close()


app = FastAPI(title="Cowork", lifespan=lifespan)


async def db_dep() -> Database:
    return app.state.db


async def conn_manager_dep() -> ConnectionManager:
    return app.state.conn_manager


async def bearer_member(
    authorization: Optional[str] = Header(default=None),
    db: Database = Depends(db_dep),
) -> tuple[str, str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    info = await db.member_for_token(token)
    if not info:
        raise HTTPException(status_code=401, detail="invalid token")
    return info


# ---- HTTP routes ----


@app.post("/projects", response_model=CreateProjectResponse)
async def create_project(req: CreateProjectRequest, db: Database = Depends(db_dep)):
    try:
        project_id, member_id, member_token, invite_token = await db.create_project(
            req.name.strip(), req.creator_display_name.strip()
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return CreateProjectResponse(
        project_id=project_id,
        member_id=member_id,
        member_token=member_token,
        default_invite_token=invite_token,
    )


@app.post("/invites/redeem", response_model=RedeemInviteResponse)
async def redeem_invite(
    req: RedeemInviteRequest,
    db: Database = Depends(db_dep),
    manager: ConnectionManager = Depends(conn_manager_dep),
):
    try:
        project, member_id, member_token = await db.redeem_invite(
            req.invite_token.strip(), req.display_name.strip()
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    new_member = await db.get_member(member_id)
    if new_member:
        await _broadcast(
            manager,
            project.id,
            {"type": "member_joined", "data": {"member": new_member.model_dump()}},
        )
    return RedeemInviteResponse(
        project_id=project.id,
        project_name=project.name,
        member_id=member_id,
        member_token=member_token,
    )


@app.post("/projects/{project_id}/invites", response_model=MintInviteResponse)
async def mint_invite(
    project_id: str,
    req: MintInviteRequest,
    member: tuple[str, str] = Depends(bearer_member),
    db: Database = Depends(db_dep),
):
    member_id, member_project = member
    if member_project != project_id:
        raise HTTPException(status_code=403, detail="not a member of this project")
    token = await db.mint_invite(project_id, member_id, req.max_uses, req.expires_in_seconds)
    return MintInviteResponse(invite_token=token)


@app.get("/projects/{project_id}/bootstrap", response_model=BootstrapResponse)
async def bootstrap(
    project_id: str,
    member: tuple[str, str] = Depends(bearer_member),
    db: Database = Depends(db_dep),
):
    member_id, member_project = member
    if member_project != project_id:
        raise HTTPException(status_code=403, detail="not a member of this project")
    project = await db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    channels = await db.list_channels(project_id)
    members = await db.list_members(project_id)
    unread = await db.unread_state(member_id, project_id)
    return BootstrapResponse(
        project=project,
        member_id=member_id,
        channels=channels,
        members=members,
        unread=unread,
    )


# ---- WebSocket ----


async def _broadcast(manager: ConnectionManager, project_id: str, frame: dict) -> None:
    for sess in manager.sessions_for(project_id):
        await sess.send(frame)


async def _handle_send_message(
    sess: ClientSession,
    db: Database,
    manager: ConnectionManager,
    data: dict,
) -> None:
    channel_id = data.get("channel_id")
    content = (data.get("content") or "").strip()
    parent_id = data.get("parent_id")
    if not channel_id or not content:
        await sess.send({"type": "error", "data": {"code": "bad_request", "message": "channel_id and content required"}})
        return
    try:
        message, mentioned = await db.post_message(channel_id, sess.member_id, content, parent_id)
    except ValueError as e:
        await sess.send({"type": "error", "data": {"code": "bad_request", "message": str(e)}})
        return
    msg_payload = message.model_dump()
    await _broadcast(manager, sess.project_id, {"type": "message", "data": {"message": msg_payload}})
    # Send mention pings to mentioned members who are connected.
    mentioned_ids = {m.id for m in mentioned}
    if mentioned_ids:
        preview = (message.content[:80] + "…") if len(message.content) > 80 else message.content
        for s in manager.sessions_for(sess.project_id):
            if s.member_id in mentioned_ids and s.focused_channel_id != channel_id:
                await s.send(
                    {
                        "type": "mention",
                        "data": {
                            "channel_id": channel_id,
                            "message_id": message.id,
                            "by_display_name": message.display_name,
                            "preview": preview,
                        },
                    }
                )


async def _handle_create_channel(
    sess: ClientSession,
    db: Database,
    manager: ConnectionManager,
    data: dict,
) -> None:
    name = (data.get("name") or "").strip()
    if not name:
        await sess.send({"type": "error", "data": {"code": "bad_request", "message": "name required"}})
        return
    try:
        channel = await db.create_channel(sess.project_id, name)
    except ValueError as e:
        await sess.send({"type": "error", "data": {"code": "bad_request", "message": str(e)}})
        return
    await _broadcast(
        manager,
        sess.project_id,
        {"type": "channel_created", "data": {"channel": channel.model_dump()}},
    )


async def _handle_mark_read(sess: ClientSession, db: Database, data: dict) -> None:
    channel_id = data.get("channel_id")
    if not channel_id:
        return
    sess.focused_channel_id = channel_id
    await db.mark_read(sess.member_id, channel_id, data.get("message_id"))
    state = await db.unread_state(sess.member_id, sess.project_id)
    update = state.get(channel_id)
    if update:
        await sess.send(
            {
                "type": "unread_update",
                "data": {
                    "channel_id": channel_id,
                    "count": update.count,
                    "mentions": update.mentions,
                },
            }
        )


async def _handle_list_history(sess: ClientSession, db: Database, data: dict) -> None:
    channel_id = data.get("channel_id")
    if not channel_id:
        return
    msgs = await db.history(
        channel_id, data.get("before_message_id"), int(data.get("limit") or 50)
    )
    await sess.send(
        {
            "type": "history",
            "data": {
                "channel_id": channel_id,
                "messages": [m.model_dump() for m in msgs],
            },
        }
    )


@app.websocket("/ws")
async def ws_endpoint(
    ws: WebSocket,
    token: str = Query(...),
    project_id: str = Query(...),
):
    db: Database = ws.app.state.db
    manager: ConnectionManager = ws.app.state.conn_manager
    info = await db.member_for_token(token)
    if not info or info[1] != project_id:
        await ws.close(code=4401)
        return
    member_id, _ = info
    await ws.accept()
    sess = ClientSession(ws, member_id, project_id)
    await manager.add(sess)
    try:
        project = await db.get_project(project_id)
        channels = await db.list_channels(project_id)
        members = await db.list_members(project_id)
        await sess.send(
            {
                "type": "hello",
                "data": {
                    "project": project.model_dump() if project else None,
                    "member_id": member_id,
                    "channels": [c.model_dump() for c in channels],
                    "members": [m.model_dump() for m in members],
                },
            }
        )
        while True:
            raw = await ws.receive_text()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                await sess.send({"type": "error", "data": {"code": "bad_json", "message": "invalid JSON"}})
                continue
            ftype = frame.get("type")
            data = frame.get("data") or {}
            if ftype == "send_message":
                await _handle_send_message(sess, db, manager, data)
            elif ftype == "create_channel":
                await _handle_create_channel(sess, db, manager, data)
            elif ftype == "mark_read":
                await _handle_mark_read(sess, db, data)
            elif ftype == "list_history":
                await _handle_list_history(sess, db, data)
            elif ftype == "ping":
                await sess.send({"type": "pong"})
            else:
                await sess.send({"type": "error", "data": {"code": "unknown_type", "message": ftype or ""}})
    except WebSocketDisconnect:
        pass
    finally:
        await manager.remove(sess)
