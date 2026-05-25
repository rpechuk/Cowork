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
from cowork.client.invite import format_invite, parse_invite
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
  /save-transcript [path]              — write the current channel transcript to a file
  /leave-project                       — remove the current project from this device
  /quit                                — exit
Type plain text to post to the current channel. Use [b]@name[/b] to mention.

[b]Selecting and copying text[/b]
  Text in the transcript is [b]always drag-selectable[/b] — just click and drag
  with your mouse, then copy with your terminal's normal key:
    macOS Terminal / iTerm2 : [b]cmd+c[/b]
    GNOME / Konsole / Kitty  : [b]ctrl+shift+c[/b]
    Windows Terminal         : [b]ctrl+shift+c[/b] or right-click → Copy

  [b]ctrl+c[/b] also copies via the OSC 52 escape and writes to
  [b]$COWORK_HOME/last-copy.txt[/b] as a fallback.

  [b]ctrl+s[/b] toggles to "Full TUI mouse mode" (hover effects + drag scroll).
  You almost never need this — it disables native text selection. The status
  bar turns [b]orange[/b] while it is active. Press [b]ctrl+s[/b] again to return.

  [b]/save-transcript[/b] dumps the entire current channel to a plain-text file.

[b]Exit[/b]
  Press [b]ctrl+q[/b] to quit, or type [b]/quit[/b].
"""


@dataclass
class ProjectState:
    cached: CachedProject
    connection: ProjectConnection
    channels: dict[str, dict] = field(default_factory=dict)
    members: dict[str, dict] = field(default_factory=dict)
    messages_by_channel: dict[str, list[dict]] = field(default_factory=dict)
    # Per-channel set of message ids whose row has already been written to the
    # transcript log. Lets us append new messages incrementally without re-
    # rendering the whole transcript (and stomping on system lines like the
    # cowork:// invite banner) every time history or a new message arrives.
    rendered_msg_ids: dict[str, set[str]] = field(default_factory=dict)
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
    #status.tui-mouse-mode { background: $warning; color: $background; text-style: bold; }
    .muted { color: $text-muted; }
    .mention { color: $warning; text-style: bold; }
    """

    # By default Cowork disables terminal mouse-drag tracking (modes 1002/1003)
    # so text in the transcript is always drag-selectable natively. Button
    # clicks (mode 1000) and scroll-wheel still work so you can click channels
    # and scroll with the wheel as normal.
    #
    # ctrl+s re-enables full TUI mouse mode (drag tracking back on) — needed
    # only if you want hover highlights or drag-based scrolling. The status bar
    # turns orange so you know you're in that mode. Press ctrl+s again to
    # return to the always-selectable default.
    #
    # ctrl+c copies the current screen selection via OSC 52.
    # ctrl+q (or /quit) exits.
    BINDINGS = [
        Binding("ctrl+s", "toggle_selection_mode", "TUI mouse mode"),
        Binding("ctrl+c", "copy_selection", "Copy selection", priority=True),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+l", "show_help", "Help"),
        Binding("ctrl+i", "focus_input", "Focus input", show=False),
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
        # Tracks whether full TUI mouse mode (drag tracking enabled) is active.
        # False (the default) means drag tracking is OFF so text is always
        # natively selectable; True means all mouse modes are on (hover, drag).
        self._mouse_capture_on: bool = False

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
        # Critical: focus the input immediately. Without this Textual defaults
        # focus to the first focusable widget (the Tree on the left), which
        # silently swallows every keystroke the user types — including the
        # /new-project command they need to connect.
        self.query_one("#input", Input).focus()
        # Disable mouse-drag tracking (xterm modes 1002/1003) so the terminal
        # does native text selection when the user drags. Mode 1000 (button
        # press/release) and mode 1006 (SGR extended coords) are kept so
        # sidebar clicks and scroll-wheel still work via Textual.
        self._disable_drag_tracking()
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

    def action_focus_input(self) -> None:
        """Snap focus back to the input field (ctrl+i)."""
        try:
            self.query_one("#input", Input).focus()
        except Exception:
            pass

    # ----- mouse-tracking helpers -----

    def _disable_drag_tracking(self) -> None:
        """Send xterm escape sequences to turn off drag/motion tracking modes
        (1002 and 1003) while leaving button-click (1000) and SGR coords (1006)
        active. This restores native terminal text selection via mouse drag."""
        driver = self._driver
        if driver is None:
            return
        try:
            driver.write("\x1b[?1002l\x1b[?1003l")
            driver.flush()
        except Exception:
            pass

    def _enable_drag_tracking(self) -> None:
        """Re-enable drag/motion tracking modes so Textual receives drag events
        (hover highlights, drag-based scroll). While active, the terminal
        forwards drag events to Textual instead of doing native text selection."""
        driver = self._driver
        if driver is None:
            return
        try:
            driver.write("\x1b[?1002h\x1b[?1003h")
            driver.flush()
        except Exception:
            pass

    def action_toggle_selection_mode(self) -> None:
        """Toggle full TUI mouse mode vs the default selection-friendly mode.

        Default (selection-friendly): xterm drag modes 1002/1003 are OFF so
        the terminal does native text selection when you drag the mouse. Button
        clicks (mode 1000) and scroll-wheel still work — click channels, scroll
        with the wheel, it all works without any toggle.

        Full TUI mouse mode (ctrl+s): re-enables drag tracking so Textual gets
        hover-highlight and drag-scroll events. While active the status bar
        turns orange and native text selection no longer works. Press ctrl+s
        again to return to the default selection-friendly state.
        """
        if self._mouse_capture_on:
            self._disable_drag_tracking()
            self._mouse_capture_on = False
            try:
                self.query_one("#status", Static).remove_class("tui-mouse-mode")
            except Exception:
                pass
            self._set_status("")
            self.notify(
                "Selection mode restored — drag to select text, copy with"
                " your terminal's copy key (cmd+c / ctrl+shift+c).",
                title="Selection mode",
                timeout=4,
            )
        else:
            self._enable_drag_tracking()
            self._mouse_capture_on = True
            try:
                self.query_one("#status", Static).add_class("tui-mouse-mode")
            except Exception:
                pass
            self._set_status(
                "■ TUI MOUSE MODE — full mouse active, text cannot be"
                " drag-selected. Press ctrl+s to restore."
            )
            self.notify(
                "Full TUI mouse mode on. Hover + drag-scroll active, but"
                " text can no longer be natively selected."
                " Press ctrl+s to restore.",
                title="TUI mouse mode",
                timeout=5,
            )

    def action_copy_selection(self) -> None:
        """Copy the current screen-level text selection to clipboard.

        Textual uses OSC 52 to ship text to the host terminal's clipboard,
        which most modern terminals honor (iTerm2, Windows Terminal, VS Code,
        Alacritty, Kitty, recent gnome-terminal). macOS Terminal.app and a
        handful of others do NOT — so we also write the copied text to a
        small file at $COWORK_HOME/last-copy.txt as a guaranteed fallback
        the user can `cat | pbcopy` (or equivalent) if their clipboard
        didn't update.
        """
        text = self.screen.get_selected_text()
        if not text:
            self.notify(
                "Nothing selected. Drag with the mouse over the transcript first.",
                severity="warning",
                title="Copy",
            )
            return
        self.copy_to_clipboard(text)
        try:
            from cowork.paths import data_dir

            path = data_dir() / "last-copy.txt"
            path.write_text(text, encoding="utf-8")
            self.notify(
                f"Copied {len(text)} chars. Backup at {path} (in case your"
                " terminal blocks OSC 52).",
                title="Copy",
                timeout=4,
            )
        except OSError:
            self.notify(f"Copied {len(text)} chars to clipboard.", title="Copy")

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
                        # Append-only: don't wipe the log. System lines written
                        # by /new-project, /invite, /help etc. stay visible.
                        self._append_new_messages(cid, msgs)
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
        # ALWAYS send mark_read on channel switch — even with no message_id —
        # so the server updates its per-session `focused_channel_id`. Without
        # this signal, mention frames for the channel we just LEFT would stop
        # being delivered to us (the server still thinks we're focused there).
        # The server's db.mark_read no-ops the last_read_at update when
        # message_id is None, so this is safe: we don't wipe unread for
        # unseen history, we only update focus.
        msgs = state.messages_by_channel.get(channel_id) or []
        last_msg_id = msgs[-1]["id"] if msgs else None
        asyncio.create_task(
            state.connection.send(
                {
                    "type": "mark_read",
                    "data": {"channel_id": channel_id, "message_id": last_msg_id},
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
        """Full re-render. Called on channel switch only. Wipes the log and
        repaints the channel header + every known message. System lines
        (e.g. the invite-URL banner from /new-project or /invite) survive
        across history arrivals because the history handler now uses
        `_append_new_messages` rather than calling this method."""
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
        rendered = state.rendered_msg_ids.setdefault(self.current_channel_id, set())
        rendered.clear()
        for m in msgs:
            log.write(self._format_message(m, state))
            rendered.add(m["id"])

    def _append_new_messages(self, channel_id: str, msgs: list[dict]) -> None:
        """Append messages that haven't been rendered yet for the current
        channel. No-op for channels not currently focused."""
        if (
            not self.current_project_id
            or self.current_channel_id != channel_id
        ):
            return
        state = self.projects[self.current_project_id]
        rendered = state.rendered_msg_ids.setdefault(channel_id, set())
        log: RichLog = self.query_one("#transcript", RichLog)
        for m in msgs:
            if m["id"] in rendered:
                continue
            log.write(self._format_message(m, state))
            rendered.add(m["id"])

    def _append_message(self, msg: dict) -> None:
        # Single-message convenience wrapper; uses the same dedupe set so
        # echoes of our own send_message don't double-print.
        if not self.current_project_id:
            return
        self._append_new_messages(msg["channel_id"], [msg])

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
        elif cmd == "save-transcript":
            await self._cmd_save_transcript(args)
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
        invite_url = format_invite(server_url, resp["default_invite_token"])
        self._write_system(
            f"Created project [b]{name}[/b]. Share either of these so others can join:\n"
            f"  [b]{invite_url}[/b]\n"
            f"  [b]/join {invite_url}[/b]"
        )

    async def _cmd_join(self, args: list[str]) -> None:
        # Accept either:   /join cowork://host:port#TOKEN [display-name]
        # or the legacy:   /join <server-url> <invite-token> [display-name]
        if len(args) == 0:
            self._write_system(
                "[red]usage: /join <cowork-url> [display-name]"
                " | /join <server-url> <invite-token> [display-name][/red]"
            )
            return
        try:
            server_url, invite_token = parse_invite(args[0])
            display_name = args[1] if len(args) > 1 else self._guess_display_name()
        except ValueError:
            if len(args) < 2:
                self._write_system(
                    "[red]usage: /join <cowork-url> [display-name]"
                    " | /join <server-url> <invite-token> [display-name][/red]"
                )
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
        invite_url = format_invite(state.cached.server_url, resp["invite_token"])
        self._write_system(
            f"Invite for [b]{state.cached.project_name}[/b]:\n"
            f"  [b]{invite_url}[/b]\n"
            f"They join with: [b]/join {invite_url}[/b]"
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

    async def _cmd_save_transcript(self, args: list[str]) -> None:
        """Write the current channel transcript to a file so users have a
        guaranteed copy-paste path even when terminal mouse capture is on."""
        if not self.current_project_id or not self.current_channel_id:
            self._write_system("[red]no channel selected[/red]")
            return
        state = self.projects[self.current_project_id]
        msgs = state.messages_by_channel.get(self.current_channel_id) or []
        channel = state.channels.get(self.current_channel_id, {})
        channel_name = channel.get("name", self.current_channel_id)
        from pathlib import Path

        from cowork.paths import data_dir

        if args:
            out_path = Path(args[0]).expanduser().resolve()
        else:
            out_dir = data_dir() / "transcripts"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{state.cached.project_name}-{channel_name}.txt"
        lines = [f"# {state.cached.project_name} / #{channel_name}", ""]
        for m in msgs:
            ts = datetime.fromtimestamp(m["created_at"]).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"[{ts}] @{m['display_name']}: {m['content']}")
        try:
            out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as e:
            self._write_system(f"[red]could not write {out_path}: {e}[/red]")
            return
        self._write_system(f"Wrote {len(msgs)} messages to [b]{out_path}[/b]")

    def _guess_display_name(self) -> str:
        import os

        return os.environ.get("USER") or os.environ.get("USERNAME") or "user"

    def action_show_help(self) -> None:
        self._write_system(HELP_TEXT)


def run() -> None:
    logging.basicConfig(level=logging.WARNING)
    CoworkApp().run()
