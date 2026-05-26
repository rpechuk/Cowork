# Cowork — project context for Claude

Multi-human, multi-agent collaborative chat. TUI client + FastAPI server,
state in SQLite, real-time fan-out over WebSockets. Phases 0–2 of the build
plan are shipping; agent integration lands later.

## Layout

```
cowork/
  cli.py                 — `cowork serve` / `cowork tui` entrypoints
  paths.py               — $COWORK_HOME resolution
  client/
    tui.py               — Textual app (CoworkApp), commands, autocomplete,
                           idle watchdog, input history, frame handlers
    conn.py              — WebSocket client + HTTP helpers
    cache.py             — SQLite cache of joined projects on the local box
    invite.py            — cowork:// URL parsing
  server/
    app.py               — FastAPI routes + WS endpoint + ConnectionManager
    db.py                — aiosqlite layer, migrations, mention resolution
    migrations/*.sql     — schema; each file applied once, tracked in
                           the _migrations table
  shared/protocol.py     — Pydantic models for HTTP/WS frames (single
                           source of truth for both ends)
tests/
  conftest.py            — in-process uvicorn fixture (`server`) +
                           ws/http helpers
  test_*.py              — scenario tests, TUI tests via Textual's pilot,
                           protocol tests, etc.
docs/protocol.md         — build-plan / phase roadmap
scripts/                 — release / utility shell scripts
```

## Conventions

- **Tests are the contract.** Anything user-visible gets a test. Server-side
  behavior is verified end-to-end against a real uvicorn instance (the
  `server` fixture spins one up per test); TUI behavior is driven with
  Textual's `app.run_test()` pilot. Avoid mocking the WS layer — round-trip
  the real protocol.
- **Single protocol source.** Don't fork frame shapes between client and
  server. Add the field to `cowork/shared/protocol.py` and let both sides
  import it.
- **Migrations are append-only.** New schema goes in a new
  `00NN_<description>.sql` file; never edit existing migrations. The runner
  in `db.py::_apply_migrations` is idempotent on already-applied names.
- **No new files unless necessary.** Prefer editing what's there. Don't
  write planning docs, scratch READMEs, or summary files unless asked.
- **Comments are for non-obvious *why*.** No restating what the code does.
  No "added for issue #X" or "used by Y" — that rots and belongs in the PR
  description.

## Common commands

```bash
# Install with test deps (mirror of what CI does):
pip install -e ".[test]"

# Run the full test suite (~50s on a clean checkout):
pytest -q

# Single test or pattern:
pytest tests/test_tui.py::test_input_history_walks_back_with_up_arrow -v
pytest -k "autocomplete" -v

# Run the server locally:
cowork serve --host 0.0.0.0 --port 8765

# Run the TUI against it:
cowork tui
```

## CI

GitHub Actions (`.github/workflows/tests.yml`) runs `pytest -q` on Python
3.11 and 3.12 for every push and every PR. Concurrency is set so a new
commit on a branch cancels the in-progress run for the previous commit.

## Anti-patterns we've already learned the hard way

- **`ctrl+1`/`ctrl+2`/etc. don't work in real terminals.** ASCII has no
  control codes for digits — most terminals send the literal digit
  regardless of ctrl. If you want panel-jump shortcuts, use F-keys or
  navigate with mouse / `ctrl+i`.
- **Setting `Input.value` programmatically doesn't move the cursor.** The
  `_submit` test helper has to set `cursor_position = len(value)` to mirror
  real typing, otherwise the autocomplete sees a partial-at-position-0 and
  swallows the next Enter.
- **The autocomplete must hide on exact matches.** Otherwise hitting Enter
  on a fully-typed command (`/help`) re-inserts the same command + a
  trailing space instead of submitting.
- **Don't broadcast back to `sess` itself on WS connect.** Single-client
  tests that drain only the `hello` frame leave the looped-back
  `member_status_changed` in the recv buffer; closing the socket then
  loses it, and downstream sequencing breaks. Broadcast only to peers.
