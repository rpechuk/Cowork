# Cowork wire protocol (v0)

All WebSocket frames are JSON objects with a required `type` field. Optional `id` is an
opaque correlation token; the server echoes it back on the response/ack when present.

The HTTP API handles project creation, invite redemption, and member-token issuance.
After redemption a client connects to `/ws?token=<member_token>&project_id=<id>`.

## HTTP endpoints

### `POST /projects`
Create a project. Body: `{ "name": "<string>", "creator_display_name": "<string>" }`.
Returns `{ "project_id", "member_id", "member_token", "default_invite_token" }`.
The default invite token can be shared so others can join.

### `POST /invites/redeem`
Redeem an invite token and create a project membership.
Body: `{ "invite_token": "<string>", "display_name": "<string>" }`.
Returns `{ "project_id", "project_name", "member_id", "member_token" }`.
Display name must be unique within the project.

### `POST /projects/{project_id}/invites`
Mint a new invite token (auth: `Authorization: Bearer <member_token>`).
Body: `{ "max_uses": <int|null>, "expires_in_seconds": <int|null> }`.
Returns `{ "invite_token" }`.

### `GET /projects/{project_id}/bootstrap`
Initial state on connect (auth: bearer member token).
Returns `{ "project", "channels", "members", "unread": { "<channel_id>": { count, mentions } } }`.

## WebSocket frames

### Client → server

| type | payload |
|---|---|
| `send_message` | `{ "channel_id", "content", "parent_id": null }` |
| `create_channel` | `{ "name" }` |
| `mark_read` | `{ "channel_id", "message_id" }` |
| `list_history` | `{ "channel_id", "before_message_id": null, "limit": 50 }` |
| `ping` | `{}` |

### Server → client

| type | payload |
|---|---|
| `hello` | `{ "project", "member_id", "channels", "members" }` (sent on connect) |
| `message` | `{ "message" }` — a `Message` row, broadcast to all project members |
| `history` | `{ "channel_id", "messages": [...] }` |
| `channel_created` | `{ "channel" }` |
| `member_joined` | `{ "member" }` |
| `mention` | `{ "channel_id", "message_id", "by_display_name", "preview" }` |
| `unread_update` | `{ "channel_id", "count", "mentions" }` |
| `error` | `{ "code", "message" }` |
| `pong` | `{}` |

## Mentions

When a `send_message` body contains `@display_name` tokens, the server resolves each
token to a project member. For every mentioned member who is not currently focused on
that channel, the server delivers a `mention` frame. The client uses `mention` frames
to ring the terminal bell. The `@` must not be preceded by an alphanumeric or
`_./-`, so substrings inside email addresses and URLs are not treated as mentions.
The author is never a recipient of their own broadcast (`@here` / `@channel`).

`@channel` and `@here` both mention every member of the project. (In a later
phase `@here` will be scoped to currently-connected members once we plumb
live presence into the DB layer; for the MVP they are equivalent.)

## Auth

The bearer token model is dead simple: redeeming an invite mints a per-member token
stored hashed on the server. The client caches the plain token locally in
`~/.cowork/client.db` so subsequent launches reconnect without prompting. Tokens are
revocable by deleting the row server-side; no expiry in MVP.
