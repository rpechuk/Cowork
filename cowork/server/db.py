from __future__ import annotations

import hashlib
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from cowork.shared.protocol import Channel, Member, Message, Project, UnreadState

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MENTION_RE = re.compile(r"@([A-Za-z0-9_][A-Za-z0-9_\-]*)")
DISPLAY_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,31}$")


def validate_display_name(name: str) -> None:
    if not DISPLAY_NAME_RE.fullmatch(name):
        raise ValueError(
            "display name must be 1-32 chars, start with a letter/digit/underscore, "
            "and contain only letters, digits, underscores, or dashes"
        )
    if name in {"here", "channel"}:
        raise ValueError(f"'{name}' is a reserved name")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(24)


def new_id() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._apply_migrations()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    async def _apply_migrations(self) -> None:
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
        )
        await self.conn.commit()
        async with self.conn.execute("SELECT name FROM _migrations") as cur:
            applied = {row["name"] async for row in cur}
        for sql_path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if sql_path.name in applied:
                continue
            await self.conn.executescript(sql_path.read_text())
            await self.conn.execute(
                "INSERT INTO _migrations (name, applied_at) VALUES (?, ?)",
                (sql_path.name, time.time()),
            )
            await self.conn.commit()

    # ---- projects, members, tokens ----

    async def create_project(self, name: str, creator_display_name: str) -> tuple[str, str, str, str]:
        if not name.strip():
            raise ValueError("project name cannot be empty")
        validate_display_name(creator_display_name)
        project_id = new_id()
        member_id = new_id()
        now = time.time()
        member_token = new_token()
        invite_token = new_token()
        await self.conn.execute(
            "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
            (project_id, name, now),
        )
        await self.conn.execute(
            "INSERT INTO project_members (id, project_id, display_name, joined_at) VALUES (?, ?, ?, ?)",
            (member_id, project_id, creator_display_name, now),
        )
        await self.conn.execute(
            "INSERT INTO member_tokens (token_hash, member_id, created_at) VALUES (?, ?, ?)",
            (hash_token(member_token), member_id, now),
        )
        await self.conn.execute(
            "INSERT INTO invite_tokens (token_hash, project_id, created_by, created_at, expires_at, max_uses, used_count)"
            " VALUES (?, ?, ?, ?, NULL, NULL, 0)",
            (hash_token(invite_token), project_id, member_id, now),
        )
        await self._create_channel(project_id, "general", now)
        await self.conn.commit()
        return project_id, member_id, member_token, invite_token

    async def mint_invite(
        self,
        project_id: str,
        created_by: str,
        max_uses: Optional[int],
        expires_in_seconds: Optional[int],
    ) -> str:
        token = new_token()
        now = time.time()
        expires_at = now + expires_in_seconds if expires_in_seconds else None
        await self.conn.execute(
            "INSERT INTO invite_tokens (token_hash, project_id, created_by, created_at, expires_at, max_uses, used_count)"
            " VALUES (?, ?, ?, ?, ?, ?, 0)",
            (hash_token(token), project_id, created_by, now, expires_at, max_uses),
        )
        await self.conn.commit()
        return token

    async def redeem_invite(self, token: str, display_name: str) -> tuple[Project, str, str]:
        validate_display_name(display_name)
        h = hash_token(token)
        async with self.conn.execute(
            "SELECT project_id, expires_at, max_uses, used_count FROM invite_tokens WHERE token_hash = ?",
            (h,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise ValueError("invalid invite token")
        if row["expires_at"] and row["expires_at"] < time.time():
            raise ValueError("invite token expired")
        if row["max_uses"] is not None and row["used_count"] >= row["max_uses"]:
            raise ValueError("invite token exhausted")
        project_id = row["project_id"]
        async with self.conn.execute(
            "SELECT 1 FROM project_members WHERE project_id = ? AND display_name = ?",
            (project_id, display_name),
        ) as cur:
            if await cur.fetchone():
                raise ValueError(f"display name '{display_name}' already taken in this project")
        member_id = new_id()
        member_token = new_token()
        now = time.time()
        try:
            await self.conn.execute(
                "INSERT INTO project_members (id, project_id, display_name, joined_at) VALUES (?, ?, ?, ?)",
                (member_id, project_id, display_name, now),
            )
        except aiosqlite.IntegrityError as e:
            # Lost a race with another concurrent redemption of the same name.
            await self.conn.rollback()
            raise ValueError(f"display name '{display_name}' already taken in this project") from e
        await self.conn.execute(
            "INSERT INTO member_tokens (token_hash, member_id, created_at) VALUES (?, ?, ?)",
            (hash_token(member_token), member_id, now),
        )
        await self.conn.execute(
            "UPDATE invite_tokens SET used_count = used_count + 1 WHERE token_hash = ?",
            (h,),
        )
        await self.conn.commit()
        project = await self.get_project(project_id)
        assert project
        return project, member_id, member_token

    async def member_for_token(self, token: str) -> Optional[tuple[str, str]]:
        async with self.conn.execute(
            "SELECT m.id, m.project_id FROM member_tokens t"
            " JOIN project_members m ON m.id = t.member_id WHERE t.token_hash = ?",
            (hash_token(token),),
        ) as cur:
            row = await cur.fetchone()
        return (row["id"], row["project_id"]) if row else None

    async def get_project(self, project_id: str) -> Optional[Project]:
        async with self.conn.execute(
            "SELECT id, name, created_at FROM projects WHERE id = ?", (project_id,)
        ) as cur:
            row = await cur.fetchone()
        return Project(**dict(row)) if row else None

    async def list_members(self, project_id: str) -> list[Member]:
        async with self.conn.execute(
            "SELECT id, project_id, display_name, joined_at FROM project_members WHERE project_id = ?"
            " ORDER BY joined_at",
            (project_id,),
        ) as cur:
            return [Member(**dict(row)) async for row in cur]

    async def get_member(self, member_id: str) -> Optional[Member]:
        async with self.conn.execute(
            "SELECT id, project_id, display_name, joined_at FROM project_members WHERE id = ?",
            (member_id,),
        ) as cur:
            row = await cur.fetchone()
        return Member(**dict(row)) if row else None

    # ---- channels ----

    async def _create_channel(self, project_id: str, name: str, ts: float) -> str:
        channel_id = new_id()
        await self.conn.execute(
            "INSERT INTO channels (id, project_id, name, created_at) VALUES (?, ?, ?, ?)",
            (channel_id, project_id, name, ts),
        )
        return channel_id

    async def create_channel(self, project_id: str, name: str) -> Channel:
        name = name.strip().lstrip("#")
        if not name:
            raise ValueError("channel name cannot be empty")
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
            raise ValueError("channel name must be alphanumeric, dashes, or underscores")
        ts = time.time()
        try:
            channel_id = await self._create_channel(project_id, name, ts)
            await self.conn.commit()
        except aiosqlite.IntegrityError as e:
            raise ValueError(f"channel '{name}' already exists") from e
        return Channel(id=channel_id, project_id=project_id, name=name, created_at=ts)

    async def list_channels(self, project_id: str) -> list[Channel]:
        async with self.conn.execute(
            "SELECT id, project_id, name, created_at FROM channels WHERE project_id = ?"
            " ORDER BY created_at",
            (project_id,),
        ) as cur:
            return [Channel(**dict(row)) async for row in cur]

    async def get_channel(self, channel_id: str) -> Optional[Channel]:
        async with self.conn.execute(
            "SELECT id, project_id, name, created_at FROM channels WHERE id = ?",
            (channel_id,),
        ) as cur:
            row = await cur.fetchone()
        return Channel(**dict(row)) if row else None

    # ---- messages ----

    async def post_message(
        self,
        channel_id: str,
        member_id: str,
        content: str,
        parent_id: Optional[str],
    ) -> tuple[Message, list[Member]]:
        channel = await self.get_channel(channel_id)
        if not channel:
            raise ValueError("channel not found")
        author = await self.get_member(member_id)
        if not author or author.project_id != channel.project_id:
            raise ValueError("member does not belong to this project")
        mentioned = await self._resolve_mentions(channel.project_id, content)
        message_id = new_id()
        ts = time.time()
        await self.conn.execute(
            "INSERT INTO messages (id, channel_id, member_id, parent_id, kind, content, created_at)"
            " VALUES (?, ?, ?, ?, 'chat', ?, ?)",
            (message_id, channel_id, member_id, parent_id, content, ts),
        )
        for m in mentioned:
            await self.conn.execute(
                "INSERT OR IGNORE INTO message_mentions (message_id, member_id) VALUES (?, ?)",
                (message_id, m.id),
            )
        await self.conn.commit()
        msg = Message(
            id=message_id,
            channel_id=channel_id,
            member_id=member_id,
            display_name=author.display_name,
            parent_id=parent_id,
            kind="chat",
            content=content,
            mentions=[m.display_name for m in mentioned],
            created_at=ts,
        )
        return msg, mentioned

    async def _resolve_mentions(self, project_id: str, content: str) -> list[Member]:
        names = {m.group(1) for m in MENTION_RE.finditer(content)}
        # MVP: @here and @channel both fan out to every project member. A future
        # phase can scope @here to currently-connected members once we plumb
        # connection-state into the DB layer.
        broadcast = bool({"here", "channel"} & names)
        names -= {"here", "channel"}
        members: list[Member] = []
        if names:
            placeholders = ",".join("?" for _ in names)
            async with self.conn.execute(
                f"SELECT id, project_id, display_name, joined_at FROM project_members"
                f" WHERE project_id = ? AND display_name IN ({placeholders})",
                (project_id, *names),
            ) as cur:
                members = [Member(**dict(row)) async for row in cur]
        if broadcast:
            all_members = await self.list_members(project_id)
            seen = {m.id for m in members}
            members.extend(m for m in all_members if m.id not in seen)
        return members

    async def history(
        self,
        channel_id: str,
        before_message_id: Optional[str],
        limit: int,
    ) -> list[Message]:
        limit = max(1, min(limit, 200))
        if before_message_id:
            async with self.conn.execute(
                "SELECT created_at FROM messages WHERE id = ?", (before_message_id,)
            ) as cur:
                row = await cur.fetchone()
            before_ts = row["created_at"] if row else None
        else:
            before_ts = None
        query = (
            "SELECT m.id, m.channel_id, m.member_id, m.parent_id, m.kind, m.content, m.created_at,"
            " pm.display_name AS display_name FROM messages m"
            " JOIN project_members pm ON pm.id = m.member_id"
            " WHERE m.channel_id = ?"
        )
        params: list[Any] = [channel_id]
        if before_ts is not None:
            query += " AND m.created_at < ?"
            params.append(before_ts)
        query += " ORDER BY m.created_at DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(query, params) as cur:
            rows = [dict(row) async for row in cur]
        rows.reverse()
        msg_ids = [r["id"] for r in rows]
        mentions_by_msg: dict[str, list[str]] = {mid: [] for mid in msg_ids}
        if msg_ids:
            placeholders = ",".join("?" for _ in msg_ids)
            async with self.conn.execute(
                f"SELECT mm.message_id, pm.display_name FROM message_mentions mm"
                f" JOIN project_members pm ON pm.id = mm.member_id"
                f" WHERE mm.message_id IN ({placeholders})",
                msg_ids,
            ) as cur:
                async for row in cur:
                    mentions_by_msg.setdefault(row["message_id"], []).append(row["display_name"])
        return [
            Message(
                id=r["id"],
                channel_id=r["channel_id"],
                member_id=r["member_id"],
                display_name=r["display_name"],
                parent_id=r["parent_id"],
                kind=r["kind"],
                content=r["content"],
                mentions=mentions_by_msg.get(r["id"], []),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ---- reads / unread ----

    async def mark_read(self, member_id: str, channel_id: str, message_id: Optional[str]) -> None:
        ts = time.time()
        await self.conn.execute(
            "INSERT INTO channel_reads (member_id, channel_id, last_read_message_id, last_read_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(member_id, channel_id) DO UPDATE SET"
            " last_read_message_id = excluded.last_read_message_id,"
            " last_read_at = excluded.last_read_at",
            (member_id, channel_id, message_id, ts),
        )
        await self.conn.commit()

    async def unread_state(self, member_id: str, project_id: str) -> dict[str, UnreadState]:
        async with self.conn.execute(
            "SELECT c.id AS channel_id, COALESCE(cr.last_read_at, 0) AS last_read_at"
            " FROM channels c"
            " LEFT JOIN channel_reads cr ON cr.channel_id = c.id AND cr.member_id = ?"
            " WHERE c.project_id = ?",
            (member_id, project_id),
        ) as cur:
            reads = {row["channel_id"]: row["last_read_at"] async for row in cur}
        result: dict[str, UnreadState] = {}
        for channel_id, last_read_at in reads.items():
            async with self.conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE channel_id = ? AND created_at > ? AND member_id != ?",
                (channel_id, last_read_at, member_id),
            ) as cur:
                row = await cur.fetchone()
                count = row["c"] if row else 0
            async with self.conn.execute(
                "SELECT COUNT(*) AS c FROM message_mentions mm"
                " JOIN messages m ON m.id = mm.message_id"
                " WHERE mm.member_id = ? AND m.channel_id = ? AND m.created_at > ?",
                (member_id, channel_id, last_read_at),
            ) as cur:
                row = await cur.fetchone()
                mentions = row["c"] if row else 0
            result[channel_id] = UnreadState(count=count, mentions=mentions)
        return result
