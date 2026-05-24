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

from cowork.paths import data_dir, server_db_path
from cowork.server.agents import AgentManager
from cowork.server.db import Database
from cowork.shared.protocol import (
    Agent,
    BootstrapResponse,
    CreateAgentRequest,
    CreateAgentResponse,
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


_NEXT_SESSION_ID = 0


def _next_session_id() -> int:
    global _NEXT_SESSION_ID
    _NEXT_SESSION_ID += 1
    return _NEXT_SESSION_ID


class ClientSession:
    def __init__(self, ws: WebSocket, member_id: str, project_id: str) -> None:
        self.id = _next_session_id()
        self.ws = ws
        self.member_id = member_id
        self.project_id = project_id
        self.focused_channel_id: Optional[str] = None

    async def send(self, payload: dict) -> None:
        try:
            await self.ws.send_text(json.dumps(payload))
        except Exception:
            logger.exception("failed to send frame")


async def _post_as_agent_factory(app: FastAPI):
    """Build the AgentManager.post_as_agent callback. The agent's reply goes
    through the same db.post_message + broadcast path as a human message,
    so other agents listening on the channel can chain off it (subject to
    the loop guard)."""

    async def post_as_agent(channel_id: str, agent_member_id: str, content: str) -> None:
        db: Database = app.state.db
        manager: ConnectionManager = app.state.conn_manager
        message, mentioned = await db.post_message(channel_id, agent_member_id, content, None)
        msg_payload = message.model_dump()
        channel = await db.get_channel(channel_id)
        if channel is None:
            return
        await _broadcast(
            manager,
            channel.project_id,
            {"type": "message", "data": {"message": msg_payload}},
        )
        mentioned_ids = {m.id for m in mentioned} - {agent_member_id}
        if mentioned_ids:
            preview = (
                (message.content[:80] + "…") if len(message.content) > 80 else message.content
            )
            targets = [
                s for s in manager.sessions_for(channel.project_id)
                if s.member_id in mentioned_ids and s.focused_channel_id != channel_id
            ]
            if targets:
                mention_frame = {
                    "type": "mention",
                    "data": {
                        "channel_id": channel_id,
                        "message_id": message.id,
                        "by_display_name": message.display_name,
                        "preview": preview,
                    },
                }
                await asyncio.gather(
                    *(s.send(mention_frame) for s in targets), return_exceptions=True
                )
        # Chain into the agent manager so other agents may respond.
        await app.state.agent_manager.on_message(message)

    return post_as_agent


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(server_db_path())
    await db.connect()
    app.state.db = db
    app.state.conn_manager = ConnectionManager()
    workspace_root = data_dir() / "workspaces"
    workspace_root.mkdir(parents=True, exist_ok=True)
    post_as_agent = await _post_as_agent_factory(app)
    app.state.agent_manager = AgentManager(
        db=db,
        workspace_root=workspace_root,
        post_as_agent=post_as_agent,
    )
    try:
        yield
    finally:
        await app.state.agent_manager.shutdown()
        await db.close()


app = FastAPI(title="Cowork", lifespan=lifespan)


async def db_dep() -> Database:
    return app.state.db


async def conn_manager_dep() -> ConnectionManager:
    return app.state.conn_manager


async def agent_manager_dep() -> AgentManager:
    return app.state.agent_manager


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
    agents = await db.list_agents(project_id)
    unread = await db.unread_state(member_id, project_id)
    return BootstrapResponse(
        project=project,
        member_id=member_id,
        channels=channels,
        members=members,
        agents=agents,
        unread=unread,
    )


@app.post("/projects/{project_id}/agents", response_model=CreateAgentResponse)
async def create_agent(
    project_id: str,
    req: CreateAgentRequest,
    member: tuple[str, str] = Depends(bearer_member),
    db: Database = Depends(db_dep),
    manager: ConnectionManager = Depends(conn_manager_dep),
):
    member_id, member_project = member
    if member_project != project_id:
        raise HTTPException(status_code=403, detail="not a member of this project")
    try:
        agent = await db.create_agent(
            project_id=project_id,
            owner_member_id=member_id,
            display_name=req.display_name.strip(),
            system_prompt=req.system_prompt,
            trigger_mode=req.trigger_mode,
            model=req.model,
            channel_id=req.channel_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    new_member = await db.get_member(agent.member_id)
    if new_member:
        await _broadcast(
            manager, project_id, {"type": "member_joined", "data": {"member": new_member.model_dump()}}
        )
    await _broadcast(
        manager, project_id, {"type": "agent_created", "data": {"agent": agent.model_dump()}}
    )
    return CreateAgentResponse(agent=agent)


@app.delete("/projects/{project_id}/agents/{agent_id}")
async def delete_agent(
    project_id: str,
    agent_id: str,
    member: tuple[str, str] = Depends(bearer_member),
    db: Database = Depends(db_dep),
    manager: ConnectionManager = Depends(conn_manager_dep),
):
    member_id, member_project = member
    if member_project != project_id:
        raise HTTPException(status_code=403, detail="not a member of this project")
    ok = await db.delete_agent(project_id, agent_id)
    if not ok:
        raise HTTPException(status_code=404, detail="agent not found")
    await _broadcast(
        manager, project_id, {"type": "agent_removed", "data": {"member_id": agent_id}}
    )
    return {"ok": True}


@app.get("/projects/{project_id}/agents", response_model=list[Agent])
async def list_agents(
    project_id: str,
    member: tuple[str, str] = Depends(bearer_member),
    db: Database = Depends(db_dep),
):
    _, member_project = member
    if member_project != project_id:
        raise HTTPException(status_code=403, detail="not a member of this project")
    return await db.list_agents(project_id)


# ---- WebSocket ----


async def _broadcast(manager: ConnectionManager, project_id: str, frame: dict) -> None:
    """Fan out concurrently so one slow socket doesn't head-of-line-block others.
    ClientSession.send already swallows per-socket exceptions internally, so
    gather will complete even if some peers have died.
    """
    sessions = manager.sessions_for(project_id)
    if sessions:
        await asyncio.gather(*(s.send(frame) for s in sessions), return_exceptions=True)


async def _handle_send_message(
    sess: ClientSession,
    db: Database,
    manager: ConnectionManager,
    agents: AgentManager,
    data: dict,
) -> None:
    channel_id = data.get("channel_id")
    content = (data.get("content") or "").strip()
    parent_id = data.get("parent_id")
    if not channel_id or not content:
        raise ValueError("channel_id and content required")
    message, mentioned = await db.post_message(channel_id, sess.member_id, content, parent_id)
    msg_payload = message.model_dump()
    await _broadcast(manager, sess.project_id, {"type": "message", "data": {"message": msg_payload}})
    # Send mention pings to mentioned members who are connected (skip the
    # author, who should never ping themselves; that case can still arise
    # when DB resolution misses the author exclusion, e.g. legacy data).
    mentioned_ids = {m.id for m in mentioned} - {sess.member_id}
    if mentioned_ids:
        preview = (message.content[:80] + "…") if len(message.content) > 80 else message.content
        targets = [
            s for s in manager.sessions_for(sess.project_id)
            if s.member_id in mentioned_ids and s.focused_channel_id != channel_id
        ]
        if targets:
            mention_frame = {
                "type": "mention",
                "data": {
                    "channel_id": channel_id,
                    "message_id": message.id,
                    "by_display_name": message.display_name,
                    "preview": preview,
                },
            }
            await asyncio.gather(
                *(s.send(mention_frame) for s in targets), return_exceptions=True
            )
    # Hand off to the agent dispatcher (does nothing if no agents are
    # configured for this channel or no owner has registered an API key).
    await agents.on_message(message)


async def _handle_create_channel(
    sess: ClientSession,
    db: Database,
    manager: ConnectionManager,
    data: dict,
) -> None:
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("name required")
    channel = await db.create_channel(sess.project_id, name)
    await _broadcast(
        manager,
        sess.project_id,
        {"type": "channel_created", "data": {"channel": channel.model_dump()}},
    )


async def _handle_mark_read(sess: ClientSession, db: Database, data: dict) -> None:
    channel_id = data.get("channel_id")
    if not channel_id:
        raise ValueError("channel_id required")
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
        raise ValueError("channel_id required")
    raw_limit = data.get("limit", 50)
    try:
        limit = int(raw_limit) if raw_limit is not None else 50
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer")
    msgs = await db.history(channel_id, data.get("before_message_id"), limit)
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
    agents: AgentManager = ws.app.state.agent_manager
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
        project_agents = await db.list_agents(project_id)
        await sess.send(
            {
                "type": "hello",
                "data": {
                    "project": project.model_dump() if project else None,
                    "member_id": member_id,
                    "channels": [c.model_dump() for c in channels],
                    "members": [m.model_dump() for m in members],
                    "agents": [a.model_dump() for a in project_agents],
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
            if not isinstance(frame, dict):
                await sess.send({"type": "error", "data": {"code": "bad_frame", "message": "frame must be a JSON object"}})
                continue
            raw_data = frame.get("data")
            data = raw_data if isinstance(raw_data, dict) else {}
            ftype = frame.get("type")
            frame_id = frame.get("id") if isinstance(frame.get("id"), str) else None
            if ftype not in {
                "send_message", "create_channel", "mark_read", "list_history", "ping",
                "register_api_key",
            }:
                err = {"type": "error", "data": {"code": "unknown_type", "message": ftype or ""}}
                if frame_id:
                    err["id"] = frame_id
                await sess.send(err)
                continue
            try:
                if ftype == "send_message":
                    await _handle_send_message(sess, db, manager, agents, data)
                elif ftype == "create_channel":
                    await _handle_create_channel(sess, db, manager, data)
                elif ftype == "mark_read":
                    await _handle_mark_read(sess, db, data)
                elif ftype == "list_history":
                    await _handle_list_history(sess, db, data)
                elif ftype == "register_api_key":
                    key = data.get("api_key")
                    if not isinstance(key, str) or not key.strip():
                        raise ValueError("api_key required")
                    agents.register_api_key(sess.member_id, sess.id, key.strip())
                    ack = {"type": "api_key_registered"}
                    if frame_id:
                        ack["id"] = frame_id
                    await sess.send(ack)
                elif ftype == "ping":
                    pong = {"type": "pong"}
                    if frame_id:
                        pong["id"] = frame_id
                    await sess.send(pong)
            except ValueError as e:
                err = {
                    "type": "error",
                    "data": {"code": "bad_request", "message": str(e)},
                }
                if frame_id:
                    err["id"] = frame_id
                await sess.send(err)
            except Exception as e:
                logger.exception("WS handler %r crashed", ftype)
                err = {
                    "type": "error",
                    "data": {"code": "internal", "message": str(e)},
                }
                if frame_id:
                    err["id"] = frame_id
                await sess.send(err)
    except WebSocketDisconnect:
        pass
    finally:
        agents.release_connection(sess.member_id, sess.id)
        await manager.remove(sess)
