from __future__ import annotations

import asyncio
import logging
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from rich.text import Text
from textual import on, events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, OptionList, RichLog, Static, Tree
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode

from cowork.client.cache import CachedProject, ClientCache
from cowork.client.conn import (
    ProjectConnection,
    ServerError,
    http_bootstrap,
    http_create_project,
    http_mint_invite,
    http_redeem_invite,
    http_register_agent,
    http_remove_agent,
)
from cowork.client.invite import format_invite, parse_invite
from cowork.paths import client_db_path
from cowork.shared.protocol import MEMBER_STATUSES

logger = logging.getLogger("cowork.tui")

DEFAULT_SERVER_URL = "http://127.0.0.1:8765"

# Status preset → (dot character, color). The dot is rendered next to each
# member's name in the right panel; the color is also used in the @mention
# autocomplete entries and in the user's own status banner.
STATUS_STYLE: dict[str, tuple[str, str]] = {
    "online": ("●", "green"),
    "away": ("●", "yellow"),
    "busy": ("●", "red"),
    "offline": ("○", "bright_black"),
}
RECENT_MENTIONS_KEEP = 5

# The slash-command autocomplete reads from this single source-of-truth list
# so the popup, the help text, and future docs all stay in sync. Each tuple
# is (command-without-slash, one-line description).
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("help", "show this help"),
    ("new-project", "create a new project on a server"),
    ("join", "join an existing project from an invite"),
    ("channel", "switch to or create a channel"),
    ("invite", "mint a fresh invite token"),
    ("status", "set presence (online/away/busy/offline)"),
    ("agent", "add / list / remove project agents"),
    ("save-transcript", "write the current channel to a file"),
    ("leave-project", "remove the current project from this device"),
    ("quit", "exit"),
]

# Auto-away defaults. The idle watchdog flips the user from online → away
# after IDLE_AWAY_THRESHOLD_S of no keyboard activity, and back to online on
# the very next keystroke. Manually setting any /status pins the status and
# disables auto-management. The check interval is short so transitions feel
# snappy; the work each tick is trivial.
IDLE_AWAY_THRESHOLD_S = 120.0
IDLE_CHECK_INTERVAL_S = 5.0

# Cap how many submitted lines we remember for the ↑/↓ history. Generous
# enough that a session's worth of commands stays reachable; small enough
# that an attacker pasting megabytes can't hold the whole transcript in
# memory through this path.
INPUT_HISTORY_MAX = 200
HELP_TEXT = """[b]Cowork commands[/b]
  Type [b]/[/b] in the input to open the command autocomplete; the menu
  filters as you type. Tab/Enter accepts the highlighted command. The full
  list:

  /help                                — show this help
  /new-project <name>                  — create a new project on a server
  /join <server-url> <invite-token>    — join an existing project
  /channel new <name>                  — create a new channel
  /channel <name>                      — switch to a channel
  /invite                              — mint a fresh invite token
  /status <online|away|busy|offline>   — set your presence (pins it; otherwise
                                          Cowork flips you to 'away' after a
                                          couple minutes idle and back on
                                          activity)
  /agent add <name> <system prompt…>   — register a Claude-Agent-SDK-backed
                                          agent in the current project; it
                                          responds whenever someone @-mentions
                                          it in any channel
  /agent list                          — show registered agents
  /agent remove <name>                 — tear down an agent
  /save-transcript [path]              — write the current channel to a file
  /leave-project                       — remove the current project from this device
  /quit                                — exit

  Plain text posts to the current channel. Use [b]@name[/b] to mention;
  an autocomplete menu appears for both [b]/[/b] commands and [b]@[/b]
  mentions — Tab/Enter accepts, Esc dismisses.

[b]Input shortcuts[/b]
  [b]ctrl+i[/b] snaps focus back to the input field.
  [b]↑/↓[/b] cycle through your previously-submitted messages and commands.

[b]Selecting and copying text[/b]
  Drag in the transcript to select. Mouse drag-tracking is off by default,
  so the terminal does native selection. Copy with your terminal's own copy
  key: [b]cmd+c[/b] (macOS), [b]ctrl+shift+c[/b] (most Linux terminals).
  [b]ctrl+s[/b] toggles full TUI mouse mode (drag tracking on) if you want
  hover effects — the status bar turns orange when active.

[b]Exit[/b]
  Press [b]ctrl+c[/b] or [b]ctrl+q[/b], or type [b]/quit[/b].
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
    # Most recent @mentions for this project (newest first), shown in the
    # left-sidebar feed. Each entry: {channel_id, by_display_name, preview, ts}.
    recent_mentions: list[dict] = field(default_factory=list)
    # Per-channel set of agent member ids currently mid-response, populated
    # by the server's agent_thinking / agent_done frames. Drives the
    # transient "🤖 @X is thinking…" status bar line.
    agent_thinking: dict[str, set[str]] = field(default_factory=dict)


class CoworkApp(App):
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #sidebar { width: 28; border-right: solid $primary 50%; }
    #mentions-feed {
        height: auto;
        max-height: 8;
        padding: 0 1;
        border-bottom: solid $primary 30%;
        background: $boost;
    }
    #mentions-feed.empty { display: none; }
    #proj-tree-wrap { height: 1fr; }
    #members { width: 24; border-left: solid $primary 50%; }
    #main { height: 1fr; }
    #transcript { height: 1fr; border: none; padding: 0 1; }
    #autocomplete {
        dock: bottom;
        height: auto;
        max-height: 8;
        background: $boost;
        border-top: solid $primary 50%;
        display: none;
    }
    #autocomplete.visible { display: block; }
    #input { dock: bottom; }
    #status { height: 1; padding: 0 1; background: $boost; color: $text; }
    #status.tui-mouse-mode { background: $warning; color: $background; text-style: bold; }
    .muted { color: $text-muted; }
    """

    # Cowork starts in "selection-friendly" mode: xterm drag modes 1002/1003
    # are OFF so text in the transcript is drag-selectable via native terminal
    # selection; mode 1000 (button clicks) and 1006 (SGR coords) stay on so
    # sidebar clicks and scroll-wheel work. ctrl+s toggles full TUI mouse
    # mode (drag tracking back on, status bar turns orange) for hover/drag
    # interactions.
    #
    # ctrl+c / ctrl+q quit. (No in-app copy binding — selecting + your
    # terminal's own copy keystroke does the job.)
    # ctrl+1/2/3 focus the sidebar / transcript / members panels.
    BINDINGS = [
        Binding("ctrl+s", "toggle_selection_mode", "TUI mouse mode"),
        Binding("ctrl+c", "quit", "Quit", priority=True),
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
        self._mouse_capture_on: bool = False
        # Autocomplete state. _ac_anchor is the index of the trigger char
        # (@ or /) in the input buffer; None when the popup is closed.
        # _ac_mode picks the completion semantics on accept.
        self._ac_anchor: Optional[int] = None
        self._ac_options: list[str] = []
        self._ac_mode: Optional[str] = None  # "mention" | "slash"
        # Idle-watchdog plumbing. Overridable from tests via the class
        # attributes so the suite can drive the transitions in ~1s instead
        # of waiting two real minutes.
        self._idle_threshold_s: float = IDLE_AWAY_THRESHOLD_S
        self._idle_check_interval_s: float = IDLE_CHECK_INTERVAL_S
        self._last_activity: float = time.time()
        # Flips to True on the user's first /status — pins them to a manual
        # presence and stops the watchdog from second-guessing them.
        self._user_set_status: bool = False
        self._idle_task: Optional[asyncio.Task] = None
        # ↑/↓ input history. _history_idx is None when the user is editing
        # a fresh draft; otherwise it's a position inside _input_history.
        # _history_draft stashes the in-progress text when the user starts
        # walking ↑, so pressing ↓ past the newest entry restores it.
        self._input_history: list[str] = []
        self._history_idx: Optional[int] = None
        self._history_draft: str = ""

    # ----- compose & mount -----

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("", id="mentions-feed", classes="empty")
                with VerticalScroll(id="proj-tree-wrap"):
                    yield Tree("Projects", id="proj-tree")
            with Vertical(id="main"):
                yield RichLog(id="transcript", highlight=False, markup=True, wrap=True)
                yield OptionList(id="autocomplete")
                yield Static("", id="status")
                yield Input(
                    placeholder="Type a message — / for command, @ to mention, ↑/↓ for history",
                    id="input",
                )
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
        # Idle watchdog: a single background task that flips presence to
        # 'away' when keyboard activity stops and back to 'online' when it
        # resumes (unless the user pinned their status manually).
        self._last_activity = time.time()
        self._idle_task = asyncio.create_task(self._idle_watchdog())
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

    # ----- presence / idle watchdog -----

    def _my_status(self) -> str:
        if not self.current_project_id:
            return "online"
        state = self.projects.get(self.current_project_id)
        if not state:
            return "online"
        me = state.members.get(state.cached.member_id)
        return (me or {}).get("status") or "online"

    async def _send_status(self, status: str) -> None:
        """Push a status change to the server for the current project. Used
        by both /status and the idle watchdog; the server broadcasts the
        change so we'll see our own status update arrive via the regular
        member_status_changed frame."""
        if not self.current_project_id:
            return
        state = self.projects.get(self.current_project_id)
        if not state:
            return
        try:
            await state.connection.send(
                {"type": "update_status", "data": {"status": status}}
            )
        except Exception:
            logger.exception("failed to push status=%s", status)

    async def _idle_watchdog(self) -> None:
        """Background loop that flips online ↔ away based on keyboard idle
        time. Skips entirely while the user has pinned their status with
        /status."""
        try:
            while True:
                await asyncio.sleep(self._idle_check_interval_s)
                if self._user_set_status:
                    continue
                if not self.current_project_id:
                    continue
                idle = time.time() - self._last_activity
                cur = self._my_status()
                if cur == "online" and idle >= self._idle_threshold_s:
                    await self._send_status("away")
                elif cur == "away" and idle < self._idle_threshold_s:
                    await self._send_status("online")
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("idle watchdog crashed")

    # ----- @ autocomplete -----

    def _autocomplete_visible(self) -> bool:
        return self._ac_anchor is not None

    def _detect_mention(self, value: str, cursor: int) -> Optional[tuple[int, str]]:
        """If the cursor is sitting inside an @<partial> token, return
        (@-index, partial-name). Returns None otherwise. Skips email-like
        substrings so '@here.com' in a URL doesn't trigger."""
        i = cursor - 1
        while i >= 0 and (value[i].isalnum() or value[i] in "_-"):
            i -= 1
        if i < 0 or value[i] != "@":
            return None
        # Same negative-lookbehind rule as MENTION_RE on the server: only
        # treat @ as a mention sigil if it sits at the start of a word.
        if i > 0 and (value[i - 1].isalnum() or value[i - 1] in "._-/"):
            return None
        return i, value[i + 1 : cursor]

    def _detect_slash(self, value: str, cursor: int) -> Optional[tuple[int, str]]:
        """If the cursor sits inside the leading /<command> token, return
        (0, partial). Slash commands are only valid at the start of the
        buffer — typing '/' mid-message is just literal text."""
        if not value.startswith("/"):
            return None
        end = 1
        while end < len(value) and (value[end].isalnum() or value[end] in "-_"):
            end += 1
        if cursor > end:
            return None
        return 0, value[1:cursor]

    def _refresh_autocomplete(self) -> None:
        """Recompute the autocomplete popup based on what the user has typed.
        Tries the slash-command popup first (only valid at start-of-input),
        then falls back to the @-mention popup."""
        try:
            inp = self.query_one("#input", Input)
            ac = self.query_one("#autocomplete", OptionList)
        except Exception:
            return
        slash = self._detect_slash(inp.value, inp.cursor_position)
        if slash is not None:
            self._populate_slash(slash[1], ac)
            return
        mention = self._detect_mention(inp.value, inp.cursor_position)
        if mention is not None:
            self._populate_mention(mention[0], mention[1], ac)
            return
        self._hide_autocomplete()

    def _populate_slash(self, partial: str, ac: OptionList) -> None:
        partial_lower = partial.lower()
        matches = [
            (cmd, desc) for cmd, desc in SLASH_COMMANDS
            if cmd.startswith(partial_lower)
        ]
        # If the user has already typed a full command name, there is
        # nothing left to autocomplete — hide so Enter submits instead of
        # re-inserting the same name with a trailing space.
        if not matches or any(cmd == partial_lower for cmd, _ in matches):
            self._hide_autocomplete()
            return
        self._ac_mode = "slash"
        self._ac_anchor = 0
        self._ac_options = [cmd for cmd, _ in matches]
        ac.clear_options()
        for cmd, desc in matches:
            label = Text()
            label.append(f"/{cmd}", style="bold cyan")
            label.append(f"  {desc}", style="dim")
            ac.add_option(Option(label, id=cmd))
        ac.add_class("visible")
        try:
            ac.highlighted = 0
        except Exception:
            pass

    def _populate_mention(self, anchor: int, partial: str, ac: OptionList) -> None:
        if not self.current_project_id:
            self._hide_autocomplete()
            return
        state = self.projects[self.current_project_id]
        my_id = state.cached.member_id
        partial_lower = partial.lower()
        # Suggest other members + @here / @channel broadcast tokens.
        names = [m["display_name"] for m in state.members.values() if m["id"] != my_id]
        if partial_lower:
            matches = [n for n in names if partial_lower in n.lower()]
        else:
            matches = names[:]
        for special in ("here", "channel"):
            if not partial_lower or partial_lower in special:
                matches.append(special)
        # If the partial already spells out one of the offered names in
        # full, hide — Enter should send the message rather than re-insert
        # the same @name with a trailing space.
        if not matches or any(name.lower() == partial_lower for name in matches):
            self._hide_autocomplete()
            return
        self._ac_mode = "mention"
        self._ac_anchor = anchor
        self._ac_options = matches
        ac.clear_options()
        for name in matches:
            ac.add_option(Option(f"@{name}", id=name))
        ac.add_class("visible")
        try:
            ac.highlighted = 0
        except Exception:
            pass

    def _hide_autocomplete(self) -> None:
        self._ac_anchor = None
        self._ac_options = []
        self._ac_mode = None
        try:
            ac = self.query_one("#autocomplete", OptionList)
            ac.remove_class("visible")
            ac.clear_options()
        except Exception:
            pass

    def _accept_autocomplete(self) -> None:
        """Replace the partial token at the anchor with the highlighted
        option (prefixed with @ for mentions, / for commands), then hide
        the popup."""
        if self._ac_anchor is None or not self._ac_options:
            return
        try:
            inp = self.query_one("#input", Input)
            ac = self.query_one("#autocomplete", OptionList)
        except Exception:
            self._hide_autocomplete()
            return
        idx = ac.highlighted if ac.highlighted is not None else 0
        if idx < 0 or idx >= len(self._ac_options):
            idx = 0
        name = self._ac_options[idx]
        anchor = self._ac_anchor
        cursor = inp.cursor_position
        sigil = "/" if self._ac_mode == "slash" else "@"
        new_value = inp.value[:anchor] + f"{sigil}{name} " + inp.value[cursor:]
        new_cursor = anchor + 1 + len(name) + 1
        self._hide_autocomplete()
        inp.value = new_value
        inp.cursor_position = new_cursor

    # ----- input history (↑/↓) -----

    def _history_back(self) -> bool:
        """Step one entry back into the submission history. The first ↑
        from a fresh draft stashes the in-progress text so ↓ can restore
        it. Returns True iff the input value actually changed (so callers
        know whether to prevent default key handling)."""
        if not self._input_history:
            return False
        try:
            inp = self.query_one("#input", Input)
        except Exception:
            return False
        if self._history_idx is None:
            self._history_draft = inp.value
            self._history_idx = len(self._input_history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        else:
            return False  # already at oldest
        inp.value = self._input_history[self._history_idx]
        inp.cursor_position = len(inp.value)
        return True

    def _history_forward(self) -> bool:
        """Step one entry forward in history. Past the newest entry we
        restore the stashed draft (or empty) and exit history-walk mode."""
        if self._history_idx is None:
            return False
        try:
            inp = self.query_one("#input", Input)
        except Exception:
            return False
        if self._history_idx < len(self._input_history) - 1:
            self._history_idx += 1
            inp.value = self._input_history[self._history_idx]
        else:
            self._history_idx = None
            inp.value = self._history_draft
            self._history_draft = ""
        inp.cursor_position = len(inp.value)
        return True

    def _autocomplete_move(self, delta: int) -> None:
        try:
            ac = self.query_one("#autocomplete", OptionList)
        except Exception:
            return
        count = ac.option_count
        if count == 0:
            return
        cur = ac.highlighted if ac.highlighted is not None else 0
        ac.highlighted = (cur + delta) % count

    @on(Input.Changed, "#input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_autocomplete()

    async def on_key(self, event: events.Key) -> None:
        """Track activity for the idle watchdog, drive the autocomplete
        popup when it's open, and step through input history with ↑/↓
        when it isn't."""
        self._last_activity = time.time()
        # Snap back to 'online' immediately on the next keystroke after
        # auto-away, rather than waiting up to IDLE_CHECK_INTERVAL_S for the
        # watchdog tick.
        if not self._user_set_status and self._my_status() == "away":
            asyncio.create_task(self._send_status("online"))
        if self._autocomplete_visible():
            if event.key == "down":
                self._autocomplete_move(1)
                event.prevent_default()
                event.stop()
            elif event.key == "up":
                self._autocomplete_move(-1)
                event.prevent_default()
                event.stop()
            elif event.key in ("tab", "enter"):
                self._accept_autocomplete()
                event.prevent_default()
                event.stop()
            elif event.key == "escape":
                self._hide_autocomplete()
                event.prevent_default()
                event.stop()
            return
        # No popup open — let ↑/↓ walk the input history.
        if self.focused is not None and self.focused.id == "input":
            if event.key == "up":
                if self._history_back():
                    event.prevent_default()
                    event.stop()
            elif event.key == "down":
                if self._history_forward():
                    event.prevent_default()
                    event.stop()

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

    async def on_unmount(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except (asyncio.CancelledError, Exception):
                pass
            self._idle_task = None
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
                    flavor = "agent" if member.get("kind") == "agent" else "joined"
                    self._write_system_in_project(
                        project_id,
                        f"[dim]→ @{member.get('display_name')} {flavor}[/dim]",
                    )
            elif ftype == "member_left":
                mid = data.get("member_id")
                if mid and mid in state.members:
                    left = state.members.pop(mid)
                    self._refresh_members_panel()
                    self._write_system_in_project(
                        project_id,
                        f"[dim]← @{left.get('display_name')} left[/dim]",
                    )
            elif ftype == "agent_thinking":
                aid = data.get("agent_id")
                cid = data.get("channel_id")
                agent = state.members.get(aid) or {}
                if aid and cid:
                    state.agent_thinking.setdefault(cid, set()).add(aid)
                    if self.current_channel_id == cid:
                        self._set_status(
                            f"🤖 @{agent.get('display_name', '?')} is thinking…"
                        )
            elif ftype == "agent_done":
                aid = data.get("agent_id")
                cid = data.get("channel_id")
                if aid and cid:
                    bucket = state.agent_thinking.get(cid)
                    if bucket:
                        bucket.discard(aid)
                        if not bucket:
                            state.agent_thinking.pop(cid, None)
                    if (
                        self.current_channel_id == cid
                        and not state.agent_thinking.get(cid)
                    ):
                        # Clear the status bar only if nothing else is
                        # currently thinking; otherwise leave whatever the
                        # most recent agent_thinking line said.
                        self._set_status("")
            elif ftype == "agent_error":
                cid = data.get("channel_id")
                name = data.get("agent_display_name", "?")
                self._write_system_in_project(
                    project_id,
                    f"[red]agent @{name} failed: {data.get('message', '?')}[/red]",
                )
            elif ftype == "mention":
                cid = data.get("channel_id")
                if cid:
                    count, mentions = state.unread.get(cid, (0, 0))
                    state.unread[cid] = (count, mentions + 1)
                    self._refresh_tree()
                self.bell()
                # Push into the persistent feed on the left sidebar; also
                # surface a one-line preview in the bottom status bar.
                state.recent_mentions.insert(
                    0,
                    {
                        "channel_id": cid,
                        "by_display_name": data.get("by_display_name", "?"),
                        "preview": data.get("preview", ""),
                        "ts": data.get("ts"),
                    },
                )
                del state.recent_mentions[RECENT_MENTIONS_KEEP:]
                if self.current_project_id == project_id:
                    self._refresh_mentions_feed()
                channel_name = state.channels.get(cid, {}).get("name", "?")
                self._set_status(
                    f"@mention from {data.get('by_display_name')} in"
                    f" #{channel_name}: {data.get('preview', '')}"
                )
            elif ftype == "member_status_changed":
                mid = data.get("member_id")
                new_status = data.get("status")
                if mid and mid in state.members and isinstance(new_status, str):
                    state.members[mid]["status"] = new_status
                    if self.current_project_id == project_id:
                        self._refresh_members_panel()
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
        self._refresh_mentions_feed()
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
            # Magenta + reverse video for the @N badge so it pops out as a
            # callout — clearly distinct from the dim unread counter below.
            text.append(f" @{mentions}", style="bold magenta reverse")
        elif count:
            text.append(f"  ({count})", style="dim")
        return text

    def _refresh_mentions_feed(self) -> None:
        """Render the recent-@mentions feed at the top of the left sidebar.
        Hides the widget entirely when the list is empty so we don't waste
        screen space on a header for nothing."""
        try:
            feed = self.query_one("#mentions-feed", Static)
        except Exception:
            return
        if not self.current_project_id:
            feed.update("")
            feed.add_class("empty")
            return
        state = self.projects[self.current_project_id]
        if not state.recent_mentions:
            feed.update("")
            feed.add_class("empty")
            return
        feed.remove_class("empty")
        lines = [Text("@mentions", style="bold magenta")]
        for m in state.recent_mentions:
            cid = m.get("channel_id")
            channel_name = state.channels.get(cid, {}).get("name", "?") if cid else "?"
            t = Text()
            t.append("• ", style="magenta")
            t.append(f"@{m.get('by_display_name', '?')}", style="bold")
            t.append(f" #{channel_name}", style="dim")
            preview = (m.get("preview") or "").strip()
            if preview:
                if len(preview) > 24:
                    preview = preview[:23] + "…"
                t.append(f": {preview}", style="dim")
            lines.append(t)
        feed.update(Text("\n").join(lines))

    def _refresh_members_panel(self) -> None:
        panel: Static = self.query_one("#members-list", Static)
        if not self.current_project_id:
            panel.update("(no project)")
            return
        state = self.projects[self.current_project_id]
        my_member = state.members.get(state.cached.member_id) or {}
        my_status = (my_member.get("status") or "online") if my_member else "online"
        # Header includes the user's own status so they can see it at a glance.
        header_text = Text("Members", style="bold underline")
        my_dot, my_color = STATUS_STYLE.get(my_status, STATUS_STYLE["online"])
        my_header = Text()
        my_header.append("you: ", style="dim")
        my_header.append(f"{my_dot} ", style=my_color)
        my_header.append(my_status, style=my_color)
        lines: list[Text] = [header_text, my_header, Text("")]
        humans = [m for m in state.members.values() if m.get("kind") != "agent"]
        agents = [m for m in state.members.values() if m.get("kind") == "agent"]
        for m in humans:
            status = m.get("status") or "online"
            dot, color = STATUS_STYLE.get(status, STATUS_STYLE["online"])
            t = Text()
            t.append(f"{dot} ", style=color)
            t.append(f"@{m['display_name']}")
            if m["id"] == state.cached.member_id:
                t.append("  (you)", style="dim")
            lines.append(t)
        if agents:
            lines.append(Text(""))
            lines.append(Text("Agents", style="bold underline"))
            # An agent is "busy" while we're awaiting its reply in any
            # channel. The members panel collapses that to a single dot;
            # the bottom status bar carries the per-channel detail.
            busy_agent_ids = {
                aid
                for bucket in state.agent_thinking.values()
                for aid in bucket
            }
            for m in agents:
                stored = m.get("status") or "online"
                effective = "busy" if m["id"] in busy_agent_ids else stored
                dot, color = STATUS_STYLE.get(
                    effective, STATUS_STYLE["online"]
                )
                t = Text()
                t.append(f"{dot} ", style=color)
                t.append("🤖 ", style="dim")
                t.append(f"@{m['display_name']}")
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
        # Always reset history navigation on submit: the next ↑ should
        # walk from the newest entry, not the middle of a previous walk.
        self._history_idx = None
        self._history_draft = ""
        if not text:
            return
        # Append to ↑/↓ history. Skip exact duplicates of the most recent
        # entry to keep the list useful (typing 'hi' three times shouldn't
        # eat three slots).
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
            if len(self._input_history) > INPUT_HISTORY_MAX:
                del self._input_history[: -INPUT_HISTORY_MAX]
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
        elif cmd == "status":
            await self._cmd_status(args)
        elif cmd == "agent":
            await self._cmd_agent(args)
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

    async def _cmd_status(self, args: list[str]) -> None:
        """Set the user's presence to one of the fixed presets. The server
        validates and rejects anything outside MEMBER_STATUSES."""
        if not self.current_project_id:
            self._write_system("[red]no project selected[/red]")
            return
        if not args:
            self._write_system(
                f"[red]usage: /status <{' | '.join(MEMBER_STATUSES)}>[/red]"
            )
            return
        new_status = args[0].strip().lower()
        if new_status not in MEMBER_STATUSES:
            self._write_system(
                f"[red]unknown status '{new_status}'. choose one of:"
                f" {', '.join(MEMBER_STATUSES)}[/red]"
            )
            return
        # Pin the user's status: the idle watchdog stops auto-managing
        # presence as soon as the user has expressed a preference.
        self._user_set_status = True
        await self._send_status(new_status)

    async def _cmd_agent(self, args: list[str]) -> None:
        """Sub-command dispatch for /agent.

        - /agent add <name> <system prompt…>   custom agent
        - /agent list                          show agents in this project
        - /agent remove <name>                 tear down an agent
        """
        if not self.current_project_id:
            self._write_system("[red]no project selected[/red]")
            return
        if not args:
            self._write_system(
                "[red]usage: /agent <add|list|remove> ...[/red]"
            )
            return
        sub, rest = args[0].lower(), args[1:]
        if sub == "add":
            await self._cmd_agent_add(rest)
        elif sub == "list":
            await self._cmd_agent_list()
        elif sub in ("remove", "rm"):
            await self._cmd_agent_remove(rest)
        else:
            self._write_system(
                f"[red]unknown agent subcommand: {sub!r}[/red] — try add/list/remove"
            )

    async def _cmd_agent_add(self, rest: list[str]) -> None:
        """`/agent add <name> <system prompt…>` — the prompt slurps every
        remaining token (shlex already merged quoted strings), so users can
        either quote the prompt or just type it after the name."""
        if len(rest) < 2:
            self._write_system(
                "[red]usage: /agent add <name> <system prompt…>[/red]"
            )
            return
        display_name = rest[0]
        system_prompt = " ".join(rest[1:]).strip()
        if not system_prompt:
            self._write_system("[red]system prompt cannot be empty[/red]")
            return
        state = self.projects[self.current_project_id]
        try:
            await http_register_agent(
                state.cached.server_url,
                state.cached.member_token,
                state.cached.project_id,
                display_name=display_name,
                system_prompt=system_prompt,
            )
        except ServerError as e:
            self._write_system(f"[red]failed to register agent: {e}[/red]")
            return
        # The member_joined broadcast updates state + members panel for
        # everyone, including this client; nothing else to do here beyond
        # acknowledging.
        self._write_system(
            f"[dim]→ agent @{display_name} registered[/dim]"
        )

    async def _cmd_agent_list(self) -> None:
        state = self.projects[self.current_project_id]
        agents = [m for m in state.members.values() if m.get("kind") == "agent"]
        if not agents:
            self._write_system(
                "[dim]no agents in this project — /agent add <name>"
                " <prompt> to create one[/dim]"
            )
            return
        lines = ["[b]Agents in this project[/b]"]
        for m in agents:
            lines.append(f"  🤖 @{m['display_name']}  ({m.get('status', 'online')})")
        self._write_system("\n".join(lines))

    async def _cmd_agent_remove(self, rest: list[str]) -> None:
        if not rest:
            self._write_system("[red]usage: /agent remove <name>[/red]")
            return
        target_name = rest[0].lstrip("@")
        state = self.projects[self.current_project_id]
        match = next(
            (
                m for m in state.members.values()
                if m.get("kind") == "agent"
                and m.get("display_name") == target_name
            ),
            None,
        )
        if not match:
            self._write_system(
                f"[red]no agent named @{target_name} in this project[/red]"
            )
            return
        try:
            await http_remove_agent(
                state.cached.server_url,
                state.cached.member_token,
                state.cached.project_id,
                agent_id=match["id"],
            )
        except ServerError as e:
            self._write_system(f"[red]failed to remove agent: {e}[/red]")

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
