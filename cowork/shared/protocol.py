from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# Fixed presence presets — clients send one of these in `update_status`. The
# server rejects anything else. Each has a fixed display color in the TUI.
MemberStatus = Literal["online", "away", "busy", "offline"]
MEMBER_STATUSES: tuple[str, ...] = ("online", "away", "busy", "offline")


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
