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

On first launch with no cached projects, the TUI prompts you to either create
or join one:

- **Create**: `/new-project mydemo http://127.0.0.1:8765 alice`
- **Join**: `/join http://server.example.com:8765 <invite-token> bob`

After joining, the TUI caches your membership locally in
`$COWORK_HOME/client.db` (or the platform user-data dir). Subsequent launches
reconnect to every cached project automatically — no re-login.

## Commands inside the TUI

| Command | Effect |
|---|---|
| `/help` | Show command reference |
| `/new-project <name> [server-url] [display-name]` | Create a project |
| `/join <server-url> <invite-token> [display-name]` | Redeem an invite |
| `/channel new <name>` | Create a channel in the current project |
| `/channel <name>` | Switch to a channel |
| `/invite` | Mint a fresh invite token for the current project |
| `/leave-project` | Remove the current project from this device only |
| `/quit` | Exit |

Plain text posts to the current channel. Use `@name` to mention; `@here` and
`@channel` are reserved.

When you are `@mentioned` in a channel you are not currently viewing, the TUI
rings the terminal bell and the channel's badge in the sidebar shows the
mention count.

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
