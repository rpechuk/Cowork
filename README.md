# Cowork

Multi-human, multi-agent collaborative chat — a TUI wrapper around the Claude
Agent SDK where conversations live under a shared **project**, organized into
**channels**, and (eventually) branch like a git DAG.

> **Status:** phases 0–4 complete.
> Server, HTTP + WS protocol, Textual TUI, project/invite flow, channels,
> cross-channel @mention notifications with terminal bell, and Claude-powered
> agents (single and multi-agent, configurable per-agent trigger mode, with a
> per-channel loop guard to stop runaway agent-to-agent chains).

## Quick start

Requires Python 3.11+.

```bash
# install (editable)
pip install -e .

# run the server (anywhere reachable by the clients)
cowork serve --host 0.0.0.0 --port 8765

# in another terminal, launch the TUI
cowork tui   # or just `cowork`
```

On first launch with no cached projects, the TUI prompts you to create or join
one. Once you launch the TUI the input box is already focused — just type:

- **Create**: `/new-project mydemo http://127.0.0.1:8765 alice`
- **Join (one-arg form)**: `/join cowork://server.example.com:8765#TOKEN`
- **Join (two-arg legacy)**: `/join http://server.example.com:8765 TOKEN bob`

When you create a project (or mint a fresh invite with `/invite`), the TUI
prints a `cowork://` URL **and** the full `/join cowork://...` command. Paste
either one in the recipient's TUI to join — no need to type the server URL
and the token separately.

After joining, the TUI caches your membership locally in
`$COWORK_HOME/client.db` (or the platform user-data dir). Subsequent launches
reconnect to every cached project automatically — no re-login.

## Commands inside the TUI

| Command | Effect |
|---|---|
| `/help` | Show command reference |
| `/new-project <name> [server-url] [display-name]` | Create a project |
| `/join <cowork-url> [display-name]` | Redeem a `cowork://...#TOKEN` invite |
| `/join <server-url> <invite-token> [display-name]` | Legacy two-arg form |
| `/channel new <name>` | Create a channel in the current project |
| `/channel <name>` | Switch to a channel |
| `/invite` | Mint a fresh invite (prints a `cowork://` URL) |
| `/api-key <sk-ant-...>` | Register your Anthropic key for the current project |
| `/agent add <name> <trigger> ["system prompt"]` | Spawn an agent in the current channel |
| `/agent list` | Show every agent in this project |
| `/agent remove <name>` | Delete an agent |
| `/save-transcript [path]` | Write the current channel to a file (easy copy) |
| `/leave-project` | Remove the current project from this device only |
| `/quit` (or `ctrl+q`) | Exit |

Plain text posts to the current channel. Use `@name` to mention; `@here` and
`@channel` are reserved.

When you are `@mentioned` in a channel you are not currently viewing, the TUI
rings the terminal bell and the channel's badge in the sidebar shows the
mention count.

### Copying text from the transcript

Drag with the mouse over the transcript. Textual highlights the
selection in-app — this is Textual's own selection mechanism, not the
terminal's, so it works regardless of what platform you're on.

Press **`ctrl+c`** to copy. Cowork ships the text via the OSC 52 escape
(honored by iTerm2, Windows Terminal, VS Code's terminal, Alacritty,
Kitty, recent gnome-terminal) AND writes it to
`$COWORK_HOME/last-copy.txt` as a guaranteed fallback for terminals
(e.g. macOS Terminal.app) that don't honor OSC 52:

```bash
cat $COWORK_HOME/last-copy.txt | pbcopy   # macOS
cat $COWORK_HOME/last-copy.txt | wl-copy  # Wayland
```

`/save-transcript [path]` is the heavier option that dumps the **entire**
current channel to a file. Use it for grep-able conversation history rather
than a single selection.

Quitting: **`ctrl+q`** (or `/quit`).

## Agents

Cowork agents are first-class chat participants powered by the
[`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/). Each agent
is a member of the project (with `is_agent` set), so it shows up in member
lists, gets its own `@display_name`, and you `@mention` it like you would a
human.

```bash
# Inside the TUI, after creating or joining a project:
/api-key sk-ant-...                           # register your Anthropic key
/agent add researcher on_mention "You are a careful researcher who cites sources."
/agent add critic    always       "Critique every claim in the chat for accuracy."
/agent list                                    # see who's in the project
@researcher what's the best way to ...?       # trigger the on_mention agent
```

Per-agent **trigger modes**:

- `always` — respond to every message (except the agent's own).
- `on_mention` — respond when `@name` appears in a message (case-insensitive).
- `on_question` — respond when the message ends in `?`.

**API key handling**: keys never touch disk on the server. Your TUI caches
its key locally and forwards it via the WebSocket on every reconnect; the
server keeps it in memory only while your session is live. When the agent's
owner disconnects, the agent goes dormant until that owner reconnects with a
key.

**Loop guard**: agent-to-agent chains stop after 4 consecutive agent
messages without a human in between. A human message in the same channel
resets the streak.

**Workspaces**: every channel gets its own scratch directory under
`$COWORK_HOME/workspaces/<channel_id>/` that's passed to the agent's
`claude-agent-sdk` invocation as `cwd`, so agents can use full Claude Code
tools (file edit, bash, web fetch) against a shared per-channel workspace.

## Protocol

See [`docs/protocol.md`](docs/protocol.md) for the HTTP + WebSocket spec.

## Roadmap

- **Phase 5** — Ceremony engine: auto-closing polls, approval gates,
  claimable tasks.
- **Phase 6** — More ceremonies: proposals, check-ins, brainstorm.
- **Phase 7** — DAG branching within a channel (the headline v1.1 feature).

## Tests

```bash
pip install -e . pytest pytest-asyncio
pytest -q
```
