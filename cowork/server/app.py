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
from cowork.server.agent_presets import PRESETS, list_presets
from cowork.server.agent_runner import AgentRunner, ClaudeSDKAgentRunner
from cowork.server.db import Database
from cowork.shared.protocol import (
    AgentConfig,
    AgentPreset,
    BootstrapResponse,
    CreateProjectRequest,
    CreateProjectResponse,
    ListPresetsResponse,
    Member,
    MintInviteRequest,
    MintInviteResponse,
    RedeemInviteRequest,
    RedeemInviteResponse,
    RegisterAgentRequest,
    RegisterAgentResponse,
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

    def is_member_connected(self, project_id: str, member_id: str) -> bool:
        """True iff member_id currently has at least one live WS in this
        project. Used to overlay 'offline' on top of each member's stored
        status preference when nobody from that member is connected."""
        bucket = self._by_project.get(project_id, ())
        return any(s.member_id == member_id for s in bucket)

    def connected_member_ids(self, project_id: str) -> set[str]:
        return {s.member_id for s in self._by_project.get(project_id, ())}


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
    # Default agent runner uses the real Claude Agent SDK. Tests replace
    # this attribute with FakeAgentRunner before kicking off the scenario,
    # so the suite never depends on the SDK binary being installed.
    app.state.agent_runner = ClaudeSDKAgentRunner()
    try:
        yield
    finally:
        await db.close()


app = FastAPI(title="Cowork", lifespan=lifespan)


async def db_dep() -> Database:
    return app.state.db


async def conn_manager_dep() -> ConnectionManager:
    return app.state.conn_manager


async def agent_runner_dep() -> AgentRunner:
    return app.state.agent_runner


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
    manager: ConnectionManager = Depends(conn_manager_dep),
):
    member_id, member_project = member
    if member_project != project_id:
        raise HTTPException(status_code=403, detail="not a member of this project")
    project = await db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    channels = await db.list_channels(project_id)
    members = await db.list_members(project_id)
    # Overlay connection state on top of stored status — but only for
    # humans. Agents are server-resident and never hold a WS, so their
    # stored status (default 'online') is their real status.
    connected = manager.connected_member_ids(project_id)
    for m in members:
        if m.kind == "human" and m.id not in connected:
            m.status = "offline"
    unread = await db.unread_state(member_id, project_id)
    return BootstrapResponse(
        project=project,
        member_id=member_id,
        channels=channels,
        members=members,
        unread=unread,
    )


@app.get("/agents/presets", response_model=ListPresetsResponse)
async def get_agent_presets():
    """List the built-in agent presets. Open endpoint — preset definitions
    aren't private and clients use this to render their menus."""
    return ListPresetsResponse(
        presets=[AgentPreset(**p) for p in list_presets()]
    )


@app.post(
    "/projects/{project_id}/agents", response_model=RegisterAgentResponse
)
async def register_agent(
    project_id: str,
    req: RegisterAgentRequest,
    member: tuple[str, str] = Depends(bearer_member),
    db: Database = Depends(db_dep),
    manager: ConnectionManager = Depends(conn_manager_dep),
):
    """Add an agent member to the project. Any existing member of the
    project can register an agent. Two modes:

    - Custom: caller supplies `display_name` and `system_prompt` directly.
    - Preset: caller supplies `preset` (a key into PRESETS); the server
      fills in system_prompt + model from the registry. `display_name`
      defaults to the preset name when omitted, so the simplest
      registration is `{"preset": "architect"}`.

    Mixing modes is allowed — any explicit field overrides the preset
    default."""
    _, member_project = member
    if member_project != project_id:
        raise HTTPException(status_code=403, detail="not a member of this project")

    preset_prompt: str | None = None
    preset_model: str | None = None
    preset_history: int | None = None
    display_name = (req.display_name or "").strip() or None
    if req.preset:
        try:
            _, preset_cfg = PRESETS[req.preset]
        except KeyError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown preset {req.preset!r}; available: "
                    f"{', '.join(sorted(PRESETS))}"
                ),
            )
        preset_prompt = preset_cfg.system_prompt
        preset_model = preset_cfg.model
        preset_history = preset_cfg.history_messages
        if display_name is None:
            display_name = req.preset

    system_prompt = (req.system_prompt or preset_prompt or "").strip()
    if not system_prompt:
        raise HTTPException(
            status_code=400,
            detail="system_prompt is required (or pass a `preset`)",
        )
    if not display_name:
        raise HTTPException(
            status_code=400, detail="display_name is required",
        )

    config = AgentConfig(
        system_prompt=system_prompt,
        model=req.model or preset_model or AgentConfig.model_fields["model"].default,
        history_messages=(
            req.history_messages
            if req.history_messages is not None
            else (preset_history if preset_history is not None
                  else AgentConfig.model_fields["history_messages"].default)
        ),
    )
    try:
        agent = await db.create_agent(project_id, display_name, config)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Fan out so every connected client sees the new agent show up in
    # their members panel without having to re-bootstrap.
    await _broadcast(
        manager,
        project_id,
        {"type": "member_joined", "data": {"member": agent.model_dump()}},
    )
    return RegisterAgentResponse(member_id=agent.id, display_name=agent.display_name)


@app.delete("/projects/{project_id}/agents/{agent_id}", status_code=204)
async def remove_agent(
    project_id: str,
    agent_id: str,
    member: tuple[str, str] = Depends(bearer_member),
    db: Database = Depends(db_dep),
    manager: ConnectionManager = Depends(conn_manager_dep),
):
    """Tear down an agent. Restricted to agents (humans can't be removed
    via this endpoint — there's no human-kick flow yet)."""
    _, member_project = member
    if member_project != project_id:
        raise HTTPException(status_code=403, detail="not a member of this project")
    target = await db.get_member(agent_id)
    if not target or target.project_id != project_id:
        raise HTTPException(status_code=404, detail="agent not found")
    if target.kind != "agent":
        raise HTTPException(status_code=400, detail="not an agent")
    deleted = await db.delete_member(agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="agent not found")
    await _broadcast(
        manager,
        project_id,
        {"type": "member_left", "data": {"member_id": agent_id}},
    )


# ---- WebSocket ----


def _members_with_presence(
    members: list, connected: set[str]
) -> list[dict]:
    """Take a list of Member rows + a set of currently-connected member IDs
    and produce dicts whose `status` reflects connection state. Humans go
    'offline' when their last WS drops; agents always carry their stored
    status (they live server-side and don't hold WebSockets at all)."""
    out: list[dict] = []
    for m in members:
        d = m.model_dump()
        if m.kind == "human" and m.id not in connected:
            d["status"] = "offline"
        out.append(d)
    return out


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
    runner: AgentRunner,
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
    # Send mention pings to human members who are connected (skip the
    # author, who should never ping themselves; that case can still arise
    # when DB resolution misses the author exclusion, e.g. legacy data).
    # Agents don't need WS pings — they react via the invocation path below.
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
    # Kick off agent responses for any mentioned agent. Each runs in its
    # own background task so the WS handler returns immediately and the
    # author's send isn't blocked by SDK latency. The author is never an
    # agent we invoke (already filtered above), so authoring loops are
    # impossible from this code path.
    invoker_name = message.display_name
    for m in mentioned:
        if m.kind == "agent" and m.id != sess.member_id:
            asyncio.create_task(
                _invoke_agent(
                    db,
                    manager,
                    runner,
                    project_id=sess.project_id,
                    channel_id=channel_id,
                    agent=m,
                    invoker_display_name=invoker_name,
                )
            )


async def _invoke_agent(
    db: Database,
    manager: ConnectionManager,
    runner: AgentRunner,
    *,
    project_id: str,
    channel_id: str,
    agent: Member,
    invoker_display_name: str,
) -> None:
    """Run one agent turn end-to-end: announce 'thinking', fetch
    transcript, call the SDK runner, post the reply as a message authored
    by the agent, then announce 'done'. Errors are surfaced to the channel
    as a system message so users always know when an agent failed instead
    of silently waiting."""
    thinking_frame = {
        "type": "agent_thinking",
        "data": {"channel_id": channel_id, "agent_id": agent.id},
    }
    done_frame = {
        "type": "agent_done",
        "data": {"channel_id": channel_id, "agent_id": agent.id},
    }
    await _broadcast(manager, project_id, thinking_frame)

    async def on_progress(kind: str, text: str) -> None:
        """Stream the runner's intermediate events to every client in the
        project. Used to drive the per-agent reasoning trail in the TUI
        (text/thinking snippet in the members panel, full transcript via
        `/agent show <name>`)."""
        await _broadcast(
            manager,
            project_id,
            {
                "type": "agent_progress",
                "data": {
                    "channel_id": channel_id,
                    "agent_id": agent.id,
                    "agent_display_name": agent.display_name,
                    "kind": kind,
                    "text": text,
                },
            },
        )

    try:
        config = await db.get_agent_config(agent.id)
        if config is None:
            raise RuntimeError(
                f"agent {agent.display_name!r} has no config row"
            )
        history = await db.history(channel_id, before_message_id=None, limit=200)
        reply_text = await runner.respond(
            config,
            agent_display_name=agent.display_name,
            history=history,
            invoker_display_name=invoker_display_name,
            on_progress=on_progress,
        )
        reply_text = (reply_text or "").strip()
        if not reply_text:
            return
        reply, _ = await db.post_message(
            channel_id, agent.id, reply_text, parent_id=None
        )
        await _broadcast(
            manager,
            project_id,
            {"type": "message", "data": {"message": reply.model_dump()}},
        )
    except Exception as e:
        logger.exception(
            "agent %r failed to respond in project=%s channel=%s",
            agent.display_name, project_id, channel_id,
        )
        await _broadcast(
            manager,
            project_id,
            {
                "type": "agent_error",
                "data": {
                    "channel_id": channel_id,
                    "agent_id": agent.id,
                    "agent_display_name": agent.display_name,
                    "message": str(e),
                },
            },
        )
    finally:
        await _broadcast(manager, project_id, done_frame)


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


async def _handle_update_status(
    sess: ClientSession,
    db: Database,
    manager: ConnectionManager,
    data: dict,
) -> None:
    status = (data.get("status") or "").strip().lower()
    if not status:
        raise ValueError("status required")
    member = await db.update_member_status(sess.member_id, status)
    await _broadcast(
        manager,
        sess.project_id,
        {
            "type": "member_status_changed",
            "data": {"member_id": member.id, "status": member.status},
        },
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
    runner: AgentRunner = ws.app.state.agent_runner
    info = await db.member_for_token(token)
    if not info or info[1] != project_id:
        await ws.close(code=4401)
        return
    member_id, _ = info
    await ws.accept()
    sess = ClientSession(ws, member_id, project_id)
    # Was this member offline (no other live sockets) right before this
    # connection landed? If yes, after we register the socket we'll
    # broadcast their stored status preference so peers see them come
    # online; if they had another tab/device already, nothing to announce.
    was_offline = not manager.is_member_connected(project_id, member_id)
    await manager.add(sess)
    try:
        project = await db.get_project(project_id)
        channels = await db.list_channels(project_id)
        members = await db.list_members(project_id)
        # Overlay 'offline' on every member with no live socket. The owner
        # of `sess` is connected (we just added them), so their stored
        # status carries through.
        connected = manager.connected_member_ids(project_id)
        members_payload = _members_with_presence(members, connected)
        await sess.send(
            {
                "type": "hello",
                "data": {
                    "project": project.model_dump() if project else None,
                    "member_id": member_id,
                    "channels": [c.model_dump() for c in channels],
                    "members": members_payload,
                },
            }
        )
        if was_offline:
            me = await db.get_member(member_id)
            if me is not None:
                # Only broadcast to peers, not to `sess` itself — the
                # hello already told them their own status, and looping
                # the message back can confuse single-client tests that
                # don't drain past the hello before sending their first
                # frame (the extra inbound frame stays in their recv
                # buffer and is lost when they close the socket).
                peers = [
                    s for s in manager.sessions_for(project_id)
                    if s is not sess
                ]
                if peers:
                    frame = {
                        "type": "member_status_changed",
                        "data": {"member_id": member_id, "status": me.status},
                    }
                    await asyncio.gather(
                        *(p.send(frame) for p in peers),
                        return_exceptions=True,
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
                "send_message", "create_channel", "mark_read",
                "list_history", "update_status", "ping",
            }:
                err = {"type": "error", "data": {"code": "unknown_type", "message": ftype or ""}}
                if frame_id:
                    err["id"] = frame_id
                await sess.send(err)
                continue
            try:
                if ftype == "send_message":
                    await _handle_send_message(sess, db, manager, runner, data)
                elif ftype == "create_channel":
                    await _handle_create_channel(sess, db, manager, data)
                elif ftype == "mark_read":
                    await _handle_mark_read(sess, db, data)
                elif ftype == "list_history":
                    await _handle_list_history(sess, db, data)
                elif ftype == "update_status":
                    await _handle_update_status(sess, db, manager, data)
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
        await manager.remove(sess)
        # If this was the last live socket for the member, peers should
        # see them flip to 'offline'. Multi-device users (another tab still
        # open) stay at their stored status.
        if not manager.is_member_connected(project_id, member_id):
            await _broadcast(
                manager,
                project_id,
                {
                    "type": "member_status_changed",
                    "data": {"member_id": member_id, "status": "offline"},
                },
            )
