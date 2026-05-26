from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# Fixed presence presets — clients send one of these in `update_status`. The
# server rejects anything else. Each has a fixed display color in the TUI.
MemberStatus = Literal["online", "away", "busy", "offline"]
MEMBER_STATUSES: tuple[str, ...] = ("online", "away", "busy", "offline")

# `kind` distinguishes a human member (driven by a WS-connected client) from
# an agent member (driven server-side by the Claude Agent SDK runner). Both
# share the same id space and display-name uniqueness rules.
MemberKind = Literal["human", "agent"]


class AgentConfig(BaseModel):
    """Persisted agent definition. Stored as JSON in the project_members
    row's agent_config column, deserialized on the server side when the
    runner needs to invoke the agent."""
    system_prompt: str
    model: str = "claude-sonnet-4-6"
    # Soft cap on conversation context the runner ships to the SDK. Pulled
    # straight from the channel transcript ending at the @mention.
    history_messages: int = 20


class Project(BaseModel):
    id: str
    name: str
    created_at: float


class Member(BaseModel):
    id: str
    project_id: str
    display_name: str
    joined_at: float
    status: MemberStatus = "online"
    kind: MemberKind = "human"


class Channel(BaseModel):
    id: str
    project_id: str
    name: str
    created_at: float


class Message(BaseModel):
    id: str
    channel_id: str
    member_id: str
    display_name: str
    parent_id: Optional[str] = None
    kind: Literal["chat", "system"] = "chat"
    content: str
    mentions: list[str] = Field(default_factory=list)
    created_at: float


class UnreadState(BaseModel):
    count: int = 0
    mentions: int = 0


# ---- HTTP request/response shapes ----


class CreateProjectRequest(BaseModel):
    name: str
    creator_display_name: str


class CreateProjectResponse(BaseModel):
    project_id: str
    member_id: str
    member_token: str
    default_invite_token: str


class RedeemInviteRequest(BaseModel):
    invite_token: str
    display_name: str


class RedeemInviteResponse(BaseModel):
    project_id: str
    project_name: str
    member_id: str
    member_token: str


class MintInviteRequest(BaseModel):
    max_uses: Optional[int] = None
    expires_in_seconds: Optional[int] = None


class MintInviteResponse(BaseModel):
    invite_token: str


class RegisterAgentRequest(BaseModel):
    """Request shape for `POST /projects/{id}/agents`.

    Two modes, distinguished by which fields are set:
      - Custom agent: `display_name` + `system_prompt` (+ optional `model`).
      - Preset agent: `preset` + optional `display_name` override. The
        server looks up the preset's system_prompt + model and uses them.
        `display_name` defaults to the preset's name when omitted.

    Mixing modes (e.g. `preset` + explicit `system_prompt`) is allowed —
    the explicit fields win.
    """

    display_name: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    history_messages: Optional[int] = None
    preset: Optional[str] = None


class RegisterAgentResponse(BaseModel):
    member_id: str
    display_name: str
    kind: MemberKind = "agent"


class AgentPreset(BaseModel):
    name: str
    description: str
    model: str


class ListPresetsResponse(BaseModel):
    presets: list[AgentPreset]


class BootstrapResponse(BaseModel):
    project: Project
    member_id: str
    channels: list[Channel]
    members: list[Member]
    unread: dict[str, UnreadState] = Field(default_factory=dict)


# ---- WebSocket frame envelopes ----


class WSFrame(BaseModel):
    type: str
    id: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)
