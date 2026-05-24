# Cowork

Multi-human, multi-agent collaborative chat — a TUI wrapper around the Claude
Agent SDK where conversations live under a shared **project**, organized into
**channels**, and (eventually) branch like a git DAG. This repo currently ships
the foundation: phases 0–2 of the [build plan](docs/protocol.md).

> **Status:** phases 0–2 complete.
> Server, HTTP + WS protocol, Textual TUI, project/invite flow, channels, and
> cross-channel @mention notifications with terminal bell. No agents yet —
> those land in phase 3+.

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
| `/save-transcript [path]` | Write the current channel to a file (easy copy) |
| `/leave-project` | Remove the current project from this device only |
| `/quit` (or `ctrl+q`) | Exit |

Plain text posts to the current channel. Use `@name` to mention; `@here` and
`@channel` are reserved.

When you are `@mentioned` in a channel you are not currently viewing, the TUI
rings the terminal bell and the channel's badge in the sidebar shows the
mention count.

### Copying text from the transcript

Drag-select with the mouse in the transcript pane, then press **`ctrl+shift+c`**
(or your terminal's normal copy keystroke). `ctrl+c` is intentionally **not**
bound to quit so it can carry the selection to your clipboard. If your terminal
still captures the mouse, hold **shift** while dragging — that bypasses
Cowork's mouse handling. As a fallback, `/save-transcript` writes the current
channel to a plain text file you can `cat` and copy.

## Protocol

See [`docs/protocol.md`](docs/protocol.md) for the HTTP + WebSocket spec.

## Roadmap

- **Phase 3** — Single agent per channel via the Claude Agent SDK, per-agent
  trigger mode (`always` / `on_mention` / `on_question`).
- **Phase 4** — Multi-agent rooms with agent-to-agent communication and a
  loop guard.
- **Phase 5** — Ceremony engine: auto-closing polls, approval gates,
  claimable tasks.
- **Phase 6** — More ceremonies: proposals, check-ins, brainstorm.
- **Phase 7** — DAG branching within a channel (the headline v1.1 feature).

## Tests

```bash
pip install -e . pytest pytest-asyncio
pytest -q
```
