from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, RichLog, Static, Tree
from textual.widgets.tree import TreeNode

from cowork.client.cache import CachedProject, ClientCache
from cowork.client.conn import (
    ProjectConnection,
    ServerError,
    http_bootstrap,
    http_create_project,
    http_mint_invite,
    http_redeem_invite,
)
from cowork.paths import client_db_path

logger = logging.getLogger("cowork.tui")

DEFAULT_SERVER_URL = "http://127.0.0.1:8765"
HELP_TEXT = """[b]Cowork commands[/b]
  /help                                — show this help
  /new-project <name>                  — create a new project on a server
  /join <server-url> <invite-token>    — join an existing project
  /channel new <name>                  — create a new channel in the current project
  /channel <name>                      — switch to a channel
  /invite                              — mint a fresh invite token for the current project
  /leave-project                       — remove the current project from this device
  /quit                                — exit
Type plain text to post to the current channel. Use [b]@name[/b] to mention.
"""


@dataclass
class ProjectState:
    cached: CachedProject
    connection: ProjectConnection
    channels: dict[str, dict] = field(default_factory=dict)
    members: dict[str, dict] = field(default_factory=dict)
    messages_by_channel: dict[str, list[dict]] = field(default_factory=dict)
    unread: dict[str, tuple[int, int]] = field(default_factory=dict)
    status: str = "connecting"


class CoworkApp(App):
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #sidebar { width: 28; border-right: solid $primary 50%; }
    #members { width: 22; border-left: solid $primary 50%; }
    #main { height: 1fr; }
    #transcript { height: 1fr; border: none; padding: 0 1; }
    #input { dock: bottom; }
    #status { height: 1; padding: 0 1; background: $boost; color: $text; }
    .muted { color: $text-muted; }
    .mention { color: $warning; text-style: bold; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("ctrl+l", "show_help", "Help"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cache = ClientCache(client_db_path())
        self.projects: dict[str, ProjectState] = {}
        self.current_project_id: Optional[str] = None
        self.current_channel_id: Optional[str] = None
        self._tree_node_for_channel: dict[str, TreeNode] = {}
        self._tree_node_for_project: dict[str, TreeNode] = {}
        self._server_url_hint = self.cache.get_state("last_server_url") or DEFAULT_SERVER_URL

    # ----- compose & mount -----

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            with VerticalScroll(id="sidebar"):
                yield Tree("Projects", id="proj-tree")
            with Vertical(id="main"):
                yield RichLog(id="transcript", highlight=False, markup=True, wrap=True)
                yield Static("", id="status")
                yield Input(placeholder="Type a message or /help", id="input")
            with VerticalScroll(id="members"):
                yield Static("(no project)", id="members-list")
        yield Footer()

    async def on_mount(self) -> None:
        tree: Tree = self.query_one("#proj-tree", Tree)
        tree.show_root = False
        tree.root.expand()
        self.title = "Cowork"
        cached = self.cache.list_projects()
        if not cached:
            self._write_system(
                "Welcome to Cowork. No projects on this device yet.\n"
                f"Default server: [b]{self._server_url_hint}[/b]\n\n"
                "Create one with:   [b]/new-project <name>[/b]\n"
                "Or join one with:  [b]/join <server-url> <invite-token>[/b]\n"
                "Type [b]/help[/b] anytime."
            )
            self._set_status("ready — no projects yet")
        else:
            for cp in cached:
                await self._attach_project(cp)
            last = self.cache.get_state("last_project_id")
            if last and last in self.projects:
                self._select_project(last)
            else:
                first = next(iter(self.projects))
                self._select_project(first)

    async def on_unmount(self) -> None:
        for state in list(self.projects.values()):
            await state.connection.stop()
        self.cache.close()

    # ----- project lifecycle -----

    async def _attach_project(self, cp: CachedProject) -> None:
        conn = ProjectConnection(
            server_url=cp.server_url,
            project_id=cp.project_id,
            member_token=cp.member_token,
            on_frame=self._make_frame_handler(cp.project_id),
            on_status=self._make_status_handler(cp.project_id),
        )
        state = ProjectState(cached=cp, connection=conn)
        self.projects[cp.project_id] = state
        try:
            data = await http_bootstrap(cp.server_url, cp.member_token, cp.project_id)
            state.channels = {c["id"]: c for c in data.get("channels", [])}
            state.members = {m["id"]: m for m in data.get("members", [])}
            for cid, u in (data.get("unread") or {}).items():
                state.unread[cid] = (int(u.get("count", 0)), int(u.get("mentions", 0)))
        except ServerError as e:
            self._write_system(f"[red]Failed to bootstrap {cp.project_name}: {e}[/red]")
        conn.start()
        self._refresh_tree()

    def _make_frame_handler(self, project_id: str):
        async def handle(ftype: str, data: dict) -> None:
            state = self.projects.get(project_id)
            if not state:
                return
            if ftype == "hello":
                state.channels = {c["id"]: c for c in data.get("channels", [])}
                state.members = {m["id"]: m for m in data.get("members", [])}
                self._refresh_tree()
                self._refresh_members_panel()
                # Auto-request history for visible channel.
                if self.current_project_id == project_id and self.current_channel_id:
                    await state.connection.send(
                        {"type": "list_history", "data": {"channel_id": self.current_channel_id, "limit": 50}}
                    )
            elif ftype == "history":
                cid = data.get("channel_id")
                if cid:
                    msgs = list(data.get("messages") or [])
                    state.messages_by_channel[cid] = msgs
                    if self.current_project_id == project_id and self.current_channel_id == cid:
                        self._render_transcript()
                        # Now that history is loaded we can record a real
                        # read marker for the channel.
                        if msgs:
                            await state.connection.send(
                                {
                                    "type": "mark_read",
                                    "data": {"channel_id": cid, "message_id": msgs[-1]["id"]},
                                }
                            )
            elif ftype == "message":
                msg = data.get("message") or {}
                cid = msg.get("channel_id")
                if not cid:
                    return
                state.messages_by_channel.setdefault(cid, []).append(msg)
                if self.current_project_id == project_id and self.current_channel_id == cid:
                    self._append_message(msg)
                    await state.connection.send(
                        {"type": "mark_read", "data": {"channel_id": cid, "message_id": msg["id"]}}
                    )
                else:
                    count, mentions = state.unread.get(cid, (0, 0))
                    state.unread[cid] = (count + 1, mentions)
                    self._refresh_tree()
            elif ftype == "channel_created":
                channel = data.get("channel") or {}
                if channel.get("id"):
                    state.channels[channel["id"]] = channel
                    self._refresh_tree()
            elif ftype == "member_joined":
                member = data.get("member") or {}
                if member.get("id"):
                    state.members[member["id"]] = member
                    self._refresh_members_panel()
                    self._write_system_in_project(
                        project_id, f"[dim]→ @{member.get('display_name')} joined[/dim]"
                    )
            elif ftype == "mention":
                cid = data.get("channel_id")
                if cid:
                    count, mentions = state.unread.get(cid, (0, 0))
                    state.unread[cid] = (count, mentions + 1)
                    self._refresh_tree()
                self.bell()
                self._set_status(
                    f"@mention from {data.get('by_display_name')} in #{state.channels.get(cid, {}).get('name', '?')}: "
                    f"{data.get('preview', '')}"
                )
            elif ftype == "unread_update":
                cid = data.get("channel_id")
                if cid:
                    state.unread[cid] = (int(data.get("count", 0)), int(data.get("mentions", 0)))
                    self._refresh_tree()
            elif ftype == "error":
                self._write_system(f"[red]error[/red]: {data.get('message', 'unknown')}")
        return handle

    def _make_status_handler(self, project_id: str):
        def handle(status: str) -> None:
            state = self.projects.get(project_id)
            if state:
                state.status = status
                if self.current_project_id == project_id:
                    self._set_status(f"{state.cached.project_name}: {status}")
        return handle

    # ----- selection & rendering -----

    def _select_project(self, project_id: str) -> None:
        state = self.projects.get(project_id)
        if not state:
            return
        self.current_project_id = project_id
        last = state.cached.last_channel_id
        if last and last in state.channels:
            self._select_channel(last)
        elif state.channels:
            first_cid = next(iter(state.channels))
            self._select_channel(first_cid)
        else:
            self.current_channel_id = None
            self._render_transcript()
            self._refresh_members_panel()
        self.cache.set_state("last_project_id", project_id)
        self.cache.set_state("last_server_url", state.cached.server_url)

    def _select_channel(self, channel_id: str) -> None:
        if not self.current_project_id:
            return
        state = self.projects[self.current_project_id]
        if channel_id not in state.channels:
            return
        self.current_channel_id = channel_id
        self.cache.touch(self.current_project_id, channel_id)
        # Clear unread for this channel locally; server will confirm.
        state.unread[channel_id] = (0, 0)
        self._refresh_tree()
        self._refresh_members_panel()
        # Fetch history if we don't have it yet, otherwise render cached.
        if channel_id not in state.messages_by_channel:
            asyncio.create_task(
                state.connection.send(
                    {"type": "list_history", "data": {"channel_id": channel_id, "limit": 50}}
                )
            )
            self._render_transcript()
        else:
            self._render_transcript()
        # Mark read on the server only if we have a concrete message to anchor
        # the read marker to. Sending mark_read with message_id=None would push
        # last_read_at to wall-clock now and wipe unread for unseen history.
        msgs = state.messages_by_channel.get(channel_id) or []
        if msgs:
            asyncio.create_task(
                state.connection.send(
                    {
                        "type": "mark_read",
                        "data": {"channel_id": channel_id, "message_id": msgs[-1]["id"]},
                    }
                )
            )

    def _refresh_tree(self) -> None:
        tree: Tree = self.query_one("#proj-tree", Tree)
        tree.clear()
        self._tree_node_for_channel.clear()
        self._tree_node_for_project.clear()
        for pid, state in self.projects.items():
            label = self._project_node_label(state)
            pnode = tree.root.add(label, data={"kind": "project", "project_id": pid}, expand=True)
            self._tree_node_for_project[pid] = pnode
            for cid, channel in state.channels.items():
                clabel = self._channel_node_label(state, cid, channel)
                cnode = pnode.add_leaf(clabel, data={"kind": "channel", "project_id": pid, "channel_id": cid})
                self._tree_node_for_channel[cid] = cnode

    def _project_node_label(self, state: ProjectState) -> Text:
        total_unread = sum(c for c, _ in state.unread.values())
        total_mentions = sum(m for _, m in state.unread.values())
        text = Text()
        active = self.current_project_id == state.cached.project_id
        text.append(state.cached.project_name, style="bold" if active else "")
        if total_mentions:
            text.append(f"  (@{total_mentions})", style="bold yellow")
        elif total_unread:
            text.append(f"  ({total_unread})", style="dim")
        return text

    def _channel_node_label(self, state: ProjectState, channel_id: str, channel: dict) -> Text:
        count, mentions = state.unread.get(channel_id, (0, 0))
        active = (
            self.current_project_id == state.cached.project_id
            and self.current_channel_id == channel_id
        )
        text = Text()
        text.append(f"  #{channel['name']}", style="bold" if active else "")
        if mentions:
            text.append(f"  (@{mentions})", style="bold yellow")
        elif count:
            text.append(f"  ({count})", style="dim")
        return text

    def _refresh_members_panel(self) -> None:
        panel: Static = self.query_one("#members-list", Static)
        if not self.current_project_id:
            panel.update("(no project)")
            return
        state = self.projects[self.current_project_id]
        lines = [Text("Members", style="bold underline"), Text("")]
        for m in state.members.values():
            t = Text()
            t.append(f"@{m['display_name']}")
            if m["id"] == state.cached.member_id:
                t.append("  (you)", style="dim")
            lines.append(t)
        panel.update(Text("\n").join(lines))

    def _render_transcript(self) -> None:
        log: RichLog = self.query_one("#transcript", RichLog)
        log.clear()
        if not self.current_project_id or not self.current_channel_id:
            log.write(Text("Select or create a channel to start chatting.", style="dim"))
            return
        state = self.projects[self.current_project_id]
        channel = state.channels.get(self.current_channel_id)
        if channel:
            log.write(Text(f"#{channel['name']}", style="bold underline"))
            log.write(Text(""))
        msgs = state.messages_by_channel.get(self.current_channel_id) or []
        for m in msgs:
            log.write(self._format_message(m, state))

    def _append_message(self, msg: dict) -> None:
        log: RichLog = self.query_one("#transcript", RichLog)
        state = self.projects[self.current_project_id] if self.current_project_id else None
        if state is None:
            return
        log.write(self._format_message(msg, state))

    def _format_message(self, msg: dict, state: ProjectState) -> Text:
        ts = datetime.fromtimestamp(msg["created_at"]).strftime("%H:%M")
        prefix = Text()
        prefix.append(f"{ts} ", style="dim")
        prefix.append(f"@{msg['display_name']}", style="bold cyan")
        prefix.append("  ")
        body = Text(msg["content"])
        my_name = state.cached.display_name
        if any(name == my_name for name in (msg.get("mentions") or [])):
            body.stylize("yellow bold")
        out = Text()
        out.append_text(prefix)
        out.append_text(body)
        return out

    def _write_system(self, message: str) -> None:
        log: RichLog = self.query_one("#transcript", RichLog)
        log.write(Text.from_markup(f"[dim]· {message}[/dim]"))

    def _write_system_in_project(self, project_id: str, message: str) -> None:
        if self.current_project_id == project_id:
            self._write_system(message)

    def _set_status(self, message: str) -> None:
        try:
            self.query_one("#status", Static).update(message)
        except Exception:
            pass

    # ----- tree clicks -----

    @on(Tree.NodeSelected)
    def _on_tree_select(self, event: Tree.NodeSelected) -> None:
        data = event.node.data or {}
        kind = data.get("kind")
        if kind == "channel":
            pid = data["project_id"]
            cid = data["channel_id"]
            if pid != self.current_project_id:
                self.current_project_id = pid
                self.cache.set_state("last_project_id", pid)
            self._select_channel(cid)
        elif kind == "project":
            self._select_project(data["project_id"])

    # ----- input -----

    @on(Input.Submitted, "#input")
    async def _on_submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            await self._handle_command(text)
        else:
            await self._send_chat(text)

    async def _send_chat(self, content: str) -> None:
        if not self.current_project_id or not self.current_channel_id:
            self._write_system("[red]No channel selected. Use /channel new <name> first.[/red]")
            return
        state = self.projects[self.current_project_id]
        await state.connection.send(
            {
                "type": "send_message",
                "data": {"channel_id": self.current_channel_id, "content": content},
            }
        )

    async def _handle_command(self, line: str) -> None:
        try:
            parts = shlex.split(line[1:])
        except ValueError as e:
            self._write_system(f"[red]bad command: {e}[/red]")
            return
        if not parts:
            return
        cmd, args = parts[0], parts[1:]
        if cmd == "help":
            self._write_system(HELP_TEXT)
        elif cmd == "quit":
            await self.action_quit()
        elif cmd == "new-project":
            await self._cmd_new_project(args)
        elif cmd == "join":
            await self._cmd_join(args)
        elif cmd == "channel":
            await self._cmd_channel(args)
        elif cmd == "invite":
            await self._cmd_invite()
        elif cmd == "leave-project":
            await self._cmd_leave_project()
        else:
            self._write_system(f"[red]unknown command: /{cmd}[/red] — try /help")

    async def _cmd_new_project(self, args: list[str]) -> None:
        if len(args) < 1:
            self._write_system("[red]usage: /new-project <name> [server-url] [display-name][/red]")
            return
        name = args[0]
        server_url = args[1] if len(args) > 1 else self._server_url_hint
        display_name = args[2] if len(args) > 2 else self._guess_display_name()
        try:
            resp = await http_create_project(server_url, name, display_name)
        except ServerError as e:
            self._write_system(f"[red]server error: {e}[/red]")
            return
        cp = CachedProject(
            project_id=resp["project_id"],
            project_name=name,
            server_url=server_url,
            member_id=resp["member_id"],
            member_token=resp["member_token"],
            display_name=display_name,
            last_used_at=0.0,
            last_channel_id=None,
        )
        self.cache.upsert_project(
            cp.project_id, cp.project_name, cp.server_url, cp.member_id,
            cp.member_token, cp.display_name, None,
        )
        self.cache.set_state("last_server_url", server_url)
        self._server_url_hint = server_url
        await self._attach_project(cp)
        self._select_project(cp.project_id)
        self._write_system(
            f"Created project [b]{name}[/b]. Invite token (share to add others): "
            f"[b]{resp['default_invite_token']}[/b]"
        )

    async def _cmd_join(self, args: list[str]) -> None:
        if len(args) < 2:
            self._write_system("[red]usage: /join <server-url> <invite-token> [display-name][/red]")
            return
        server_url, invite_token = args[0], args[1]
        display_name = args[2] if len(args) > 2 else self._guess_display_name()
        try:
            resp = await http_redeem_invite(server_url, invite_token, display_name)
        except ServerError as e:
            self._write_system(f"[red]server error: {e}[/red]")
            return
        cp = CachedProject(
            project_id=resp["project_id"],
            project_name=resp["project_name"],
            server_url=server_url,
            member_id=resp["member_id"],
            member_token=resp["member_token"],
            display_name=display_name,
            last_used_at=0.0,
            last_channel_id=None,
        )
        self.cache.upsert_project(
            cp.project_id, cp.project_name, cp.server_url, cp.member_id,
            cp.member_token, cp.display_name, None,
        )
        self.cache.set_state("last_server_url", server_url)
        self._server_url_hint = server_url
        await self._attach_project(cp)
        self._select_project(cp.project_id)
        self._write_system(f"Joined [b]{resp['project_name']}[/b] as @{display_name}.")

    async def _cmd_channel(self, args: list[str]) -> None:
        if not self.current_project_id:
            self._write_system("[red]no project selected[/red]")
            return
        state = self.projects[self.current_project_id]
        if args and args[0] == "new":
            if len(args) < 2:
                self._write_system("[red]usage: /channel new <name>[/red]")
                return
            await state.connection.send({"type": "create_channel", "data": {"name": args[1]}})
            return
        if not args:
            self._write_system("[red]usage: /channel <name> | /channel new <name>[/red]")
            return
        target = args[0].lstrip("#")
        for cid, ch in state.channels.items():
            if ch["name"] == target:
                self._select_channel(cid)
                return
        self._write_system(f"[red]no channel named #{target}[/red]")

    async def _cmd_invite(self) -> None:
        if not self.current_project_id:
            self._write_system("[red]no project selected[/red]")
            return
        state = self.projects[self.current_project_id]
        try:
            resp = await http_mint_invite(
                state.cached.server_url, state.cached.member_token, state.cached.project_id
            )
        except ServerError as e:
            self._write_system(f"[red]server error: {e}[/red]")
            return
        self._write_system(
            f"Invite token for [b]{state.cached.project_name}[/b]: [b]{resp['invite_token']}[/b]\n"
            f"They join with: /join {state.cached.server_url} {resp['invite_token']}"
        )

    async def _cmd_leave_project(self) -> None:
        if not self.current_project_id:
            return
        pid = self.current_project_id
        state = self.projects.pop(pid)
        await state.connection.stop()
        self.cache.remove_project(pid)
        self.current_project_id = None
        self.current_channel_id = None
        if self.projects:
            self._select_project(next(iter(self.projects)))
        else:
            self._render_transcript()
            self._refresh_members_panel()
        self._refresh_tree()
        self._write_system(f"Left {state.cached.project_name} on this device.")

    def _guess_display_name(self) -> str:
        import os

        return os.environ.get("USER") or os.environ.get("USERNAME") or "user"

    def action_show_help(self) -> None:
        self._write_system(HELP_TEXT)


def run() -> None:
    logging.basicConfig(level=logging.WARNING)
    CoworkApp().run()
