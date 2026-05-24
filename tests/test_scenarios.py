"""End-to-end scenario tests for phases 0-2.

Organized by feature area:
  - sharing & joining a project
  - chatting and history persistence
  - multi-channel isolation
  - cross-channel @mention notifications + unread state
  - validation, auth, and error paths
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import websockets

from tests.conftest import (
    assert_no_frame,
    bootstrap,
    create_project,
    drain,
    recv_frame,
    redeem_invite,
    send,
    ws_url,
)


# ---------------------------------------------------------------------------
# Sharing & joining
# ---------------------------------------------------------------------------


async def test_create_project_returns_default_invite(client: httpx.AsyncClient) -> None:
    alice = await create_project(client, "demo", "alice")
    assert alice["project_id"]
    assert alice["member_id"]
    assert alice["member_token"]
    assert alice["default_invite_token"]


async def test_invite_token_lets_a_second_member_join(client: httpx.AsyncClient) -> None:
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    assert bob["project_id"] == alice["project_id"]
    assert bob["project_name"] == "demo"
    assert bob["member_token"] != alice["member_token"]


async def test_member_can_mint_additional_invites(client: httpx.AsyncClient) -> None:
    alice = await create_project(client, "demo", "alice")
    r = await client.post(
        f"/projects/{alice['project_id']}/invites",
        headers={"Authorization": f"Bearer {alice['member_token']}"},
        json={"max_uses": None, "expires_in_seconds": None},
    )
    assert r.status_code == 200
    token = r.json()["invite_token"]
    bob = await redeem_invite(client, token, "bob")
    assert bob["project_id"] == alice["project_id"]


async def test_bootstrap_returns_all_members_and_default_channel(
    client: httpx.AsyncClient,
) -> None:
    alice = await create_project(client, "demo", "alice")
    await redeem_invite(client, alice["default_invite_token"], "bob")
    data = await bootstrap(client, alice["member_token"], alice["project_id"])
    assert {m["display_name"] for m in data["members"]} == {"alice", "bob"}
    assert [c["name"] for c in data["channels"]] == ["general"]


async def test_existing_members_see_member_joined_when_new_member_redeems(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        await recv_frame(ws, "hello")
        # Bob joins via HTTP while Alice is connected.
        await redeem_invite(client, alice["default_invite_token"], "bob")
        frame = await recv_frame(ws, "member_joined")
        assert frame["data"]["member"]["display_name"] == "bob"


# ---------------------------------------------------------------------------
# Chatting + history
# ---------------------------------------------------------------------------


async def test_two_members_see_each_others_messages_live(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a, \
               websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b:
        hello_a = await recv_frame(ws_a, "hello")
        await recv_frame(ws_b, "hello")
        general_id = hello_a["data"]["channels"][0]["id"]

        await send(ws_a, "send_message", channel_id=general_id, content="hello from alice")
        msg_a = await recv_frame(ws_a, "message")
        msg_b = await recv_frame(ws_b, "message")
        assert msg_a["data"]["message"]["id"] == msg_b["data"]["message"]["id"]
        assert msg_b["data"]["message"]["display_name"] == "alice"

        await send(ws_b, "send_message", channel_id=general_id, content="hi alice")
        await recv_frame(ws_a, "message")
        await recv_frame(ws_b, "message")


async def test_history_persists_across_reconnect(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        for content in ["one", "two", "three"]:
            await send(ws, "send_message", channel_id=general_id, content=content)
            await recv_frame(ws, "message")

    # Reconnect; history endpoint should replay the messages in order.
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
        await recv_frame(ws, "hello")
        await send(ws, "list_history", channel_id=general_id, limit=50)
        history = await recv_frame(ws, "history")
        assert [m["content"] for m in history["data"]["messages"]] == ["one", "two", "three"]


async def test_late_joiner_can_read_full_history(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "team", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    pid = alice["project_id"]

    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a, \
               websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b:
        hello = await recv_frame(ws_a, "hello")
        await recv_frame(ws_b, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        scripted = [(ws_a, "hi from alice"), (ws_b, "hi from bob"), (ws_a, "how are you")]
        for ws_, text in scripted:
            await send(ws_, "send_message", channel_id=general_id, content=text)
            await recv_frame(ws_a, "message")
            await recv_frame(ws_b, "message")

    # Carol joins after the conversation and can replay it.
    carol = await redeem_invite(client, alice["default_invite_token"], "carol")
    async with websockets.connect(ws_url(server, carol["member_token"], pid)) as ws_c:
        hello = await recv_frame(ws_c, "hello")
        assert {m["display_name"] for m in hello["data"]["members"]} == {"alice", "bob", "carol"}
        await send(ws_c, "list_history", channel_id=general_id, limit=50)
        history = await recv_frame(ws_c, "history")
        seen = [(m["display_name"], m["content"]) for m in history["data"]["messages"]]
        assert seen == [("alice", "hi from alice"), ("bob", "hi from bob"), ("alice", "how are you")]


async def test_history_respects_limit_and_pagination(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        for i in range(5):
            await send(ws, "send_message", channel_id=general_id, content=f"m{i}")
            await recv_frame(ws, "message")

        await send(ws, "list_history", channel_id=general_id, limit=3)
        page1 = (await recv_frame(ws, "history"))["data"]["messages"]
        assert [m["content"] for m in page1] == ["m2", "m3", "m4"]

        # Paginate backwards using the oldest id we have.
        await send(
            ws,
            "list_history",
            channel_id=general_id,
            limit=10,
            before_message_id=page1[0]["id"],
        )
        page2 = (await recv_frame(ws, "history"))["data"]["messages"]
        assert [m["content"] for m in page2] == ["m0", "m1"]


# ---------------------------------------------------------------------------
# Multi-channel isolation
# ---------------------------------------------------------------------------


async def test_create_channel_broadcasts_to_all_members(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a, \
               websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b:
        await recv_frame(ws_a, "hello")
        await recv_frame(ws_b, "hello")
        await send(ws_a, "create_channel", name="random")
        a_event = await recv_frame(ws_a, "channel_created")
        b_event = await recv_frame(ws_b, "channel_created")
        assert a_event["data"]["channel"]["id"] == b_event["data"]["channel"]["id"]
        assert b_event["data"]["channel"]["name"] == "random"


async def test_messages_are_isolated_per_channel(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws, "create_channel", name="random")
        ch = await recv_frame(ws, "channel_created")
        random_id = ch["data"]["channel"]["id"]

        await send(ws, "send_message", channel_id=general_id, content="in general")
        await recv_frame(ws, "message")
        await send(ws, "send_message", channel_id=random_id, content="in random")
        await recv_frame(ws, "message")

        await send(ws, "list_history", channel_id=general_id, limit=50)
        general_hist = (await recv_frame(ws, "history"))["data"]["messages"]
        await send(ws, "list_history", channel_id=random_id, limit=50)
        random_hist = (await recv_frame(ws, "history"))["data"]["messages"]
        assert [m["content"] for m in general_hist] == ["in general"]
        assert [m["content"] for m in random_hist] == ["in random"]


async def test_channel_name_strips_leading_hash(client: httpx.AsyncClient, server: str) -> None:
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        await recv_frame(ws, "hello")
        await send(ws, "create_channel", name="#with-hash")
        ev = await recv_frame(ws, "channel_created")
        assert ev["data"]["channel"]["name"] == "with-hash"


# ---------------------------------------------------------------------------
# Mentions, notifications, unread state
# ---------------------------------------------------------------------------


async def test_mention_in_unfocused_channel_delivers_mention_frame(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a, \
               websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b:
        hello = await recv_frame(ws_a, "hello")
        await recv_frame(ws_b, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws_a, "create_channel", name="random")
        random_id = (await recv_frame(ws_a, "channel_created"))["data"]["channel"]["id"]
        await recv_frame(ws_b, "channel_created")

        # Bob is focused on #general.
        await send(ws_b, "mark_read", channel_id=general_id)
        await drain(ws_b)

        await send(ws_a, "send_message", channel_id=random_id, content="hey @bob look")
        mention = await recv_frame(ws_b, "mention")
        assert mention["data"]["channel_id"] == random_id
        assert mention["data"]["by_display_name"] == "alice"
        assert "bob" in mention["data"]["preview"]


async def test_mention_in_focused_channel_emits_no_mention_frame(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a, \
               websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b:
        hello = await recv_frame(ws_a, "hello")
        await recv_frame(ws_b, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        # Bob says he's currently looking at #general.
        await send(ws_b, "mark_read", channel_id=general_id)
        await drain(ws_b)

        await send(ws_a, "send_message", channel_id=general_id, content="@bob hi")
        # Bob receives the regular message...
        msg = await recv_frame(ws_b, "message")
        assert msg["data"]["message"]["mentions"] == ["bob"]
        # ...but no separate mention notification since he's focused there.
        await assert_no_frame(ws_b, "mention", within=0.2)


async def test_at_channel_mentions_every_member(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    carol = await redeem_invite(client, alice["default_invite_token"], "carol")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a, \
               websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b, \
               websockets.connect(ws_url(server, carol["member_token"], pid)) as ws_c:
        hello = await recv_frame(ws_a, "hello")
        await recv_frame(ws_b, "hello")
        await recv_frame(ws_c, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws_a, "create_channel", name="random")
        random_id = (await recv_frame(ws_a, "channel_created"))["data"]["channel"]["id"]
        await recv_frame(ws_b, "channel_created")
        await recv_frame(ws_c, "channel_created")

        # Bob and Carol are focused on #general.
        await send(ws_b, "mark_read", channel_id=general_id)
        await send(ws_c, "mark_read", channel_id=general_id)
        await drain(ws_b)
        await drain(ws_c)

        await send(ws_a, "send_message", channel_id=random_id, content="@channel everyone please")
        msg = await recv_frame(ws_a, "message")
        # The author is excluded from the broadcast recipient set so they
        # don't ping themselves.
        assert set(msg["data"]["message"]["mentions"]) == {"bob", "carol"}
        bob_mention = await recv_frame(ws_b, "mention")
        carol_mention = await recv_frame(ws_c, "mention")
        assert bob_mention["data"]["channel_id"] == random_id
        assert carol_mention["data"]["channel_id"] == random_id


async def test_at_here_behaves_like_at_channel(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a, \
               websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b:
        hello = await recv_frame(ws_a, "hello")
        await recv_frame(ws_b, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws_a, "create_channel", name="random")
        random_id = (await recv_frame(ws_a, "channel_created"))["data"]["channel"]["id"]
        await recv_frame(ws_b, "channel_created")
        await send(ws_b, "mark_read", channel_id=general_id)
        await drain(ws_b)

        await send(ws_a, "send_message", channel_id=random_id, content="@here standup")
        await recv_frame(ws_b, "mention")


async def test_mark_read_clears_unread_state(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a, \
               websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b:
        hello = await recv_frame(ws_a, "hello")
        await recv_frame(ws_b, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws_a, "create_channel", name="random")
        random_id = (await recv_frame(ws_a, "channel_created"))["data"]["channel"]["id"]
        await recv_frame(ws_b, "channel_created")

        # Bob focused elsewhere; Alice posts mentions in #random. Track the
        # ids so Bob can mark_read against a real anchor.
        await send(ws_b, "mark_read", channel_id=general_id)
        await drain(ws_b)
        sent_ids: list[str] = []
        for content in ["@bob 1", "@bob 2", "plain"]:
            await send(ws_a, "send_message", channel_id=random_id, content=content)
            sent_ids.append((await recv_frame(ws_a, "message"))["data"]["message"]["id"])
        await drain(ws_b)

        state = await bootstrap(client, bob["member_token"], pid)
        random_state = state["unread"][random_id]
        assert random_state["count"] == 3
        assert random_state["mentions"] == 2

        # Bob reads #random up to the latest message; counts reset.
        await send(ws_b, "mark_read", channel_id=random_id, message_id=sent_ids[-1])
        await recv_frame(ws_b, "unread_update")
        state = await bootstrap(client, bob["member_token"], pid)
        assert state["unread"][random_id]["count"] == 0
        assert state["unread"][random_id]["mentions"] == 0


async def test_own_messages_dont_count_as_unread(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws, "send_message", channel_id=general_id, content="me me me")
        await recv_frame(ws, "message")

    state = await bootstrap(client, alice["member_token"], pid)
    # Alice never called mark_read but her own message shouldn't show as unread.
    assert state["unread"][general_id]["count"] == 0


# ---------------------------------------------------------------------------
# Validation, auth, error paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["bad name", "with!exclaim", "über", "", "-leading-dash", "here", "channel", "x" * 33],
)
async def test_invalid_display_names_rejected(
    client: httpx.AsyncClient, name: str
) -> None:
    r = await client.post(
        "/projects",
        json={"name": "demo", "creator_display_name": name},
    )
    assert r.status_code == 400, name


async def test_duplicate_display_name_in_project_rejected(
    client: httpx.AsyncClient,
) -> None:
    alice = await create_project(client, "demo", "alice")
    r = await client.post(
        "/invites/redeem",
        json={"invite_token": alice["default_invite_token"], "display_name": "alice"},
    )
    assert r.status_code == 400


async def test_same_display_name_allowed_across_projects(
    client: httpx.AsyncClient,
) -> None:
    a1 = await create_project(client, "p1", "alice")
    a2 = await create_project(client, "p2", "alice")
    assert a1["project_id"] != a2["project_id"]
    assert a1["member_id"] != a2["member_id"]


async def test_invalid_invite_token_rejected(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/invites/redeem",
        json={"invite_token": "not-a-real-token", "display_name": "bob"},
    )
    assert r.status_code == 400


async def test_invite_max_uses_enforced(client: httpx.AsyncClient) -> None:
    alice = await create_project(client, "demo", "alice")
    r = await client.post(
        f"/projects/{alice['project_id']}/invites",
        headers={"Authorization": f"Bearer {alice['member_token']}"},
        json={"max_uses": 1, "expires_in_seconds": None},
    )
    token = r.json()["invite_token"]
    await redeem_invite(client, token, "bob")
    r = await client.post(
        "/invites/redeem",
        json={"invite_token": token, "display_name": "carol"},
    )
    assert r.status_code == 400
    assert "exhausted" in r.text


async def test_invite_expiry_enforced(client: httpx.AsyncClient) -> None:
    alice = await create_project(client, "demo", "alice")
    r = await client.post(
        f"/projects/{alice['project_id']}/invites",
        headers={"Authorization": f"Bearer {alice['member_token']}"},
        json={"max_uses": None, "expires_in_seconds": -1},
    )
    token = r.json()["invite_token"]
    r = await client.post(
        "/invites/redeem",
        json={"invite_token": token, "display_name": "bob"},
    )
    assert r.status_code == 400
    assert "expired" in r.text


async def test_bootstrap_requires_bearer_token(client: httpx.AsyncClient) -> None:
    alice = await create_project(client, "demo", "alice")
    r = await client.get(f"/projects/{alice['project_id']}/bootstrap")
    assert r.status_code == 401


async def test_bootstrap_wrong_project_forbidden(client: httpx.AsyncClient) -> None:
    p1 = await create_project(client, "p1", "alice")
    p2 = await create_project(client, "p2", "alice")
    r = await client.get(
        f"/projects/{p2['project_id']}/bootstrap",
        headers={"Authorization": f"Bearer {p1['member_token']}"},
    )
    assert r.status_code == 403


async def test_mint_invite_requires_membership(client: httpx.AsyncClient) -> None:
    p1 = await create_project(client, "p1", "alice")
    p2 = await create_project(client, "p2", "bob")
    r = await client.post(
        f"/projects/{p2['project_id']}/invites",
        headers={"Authorization": f"Bearer {p1['member_token']}"},
        json={"max_uses": None, "expires_in_seconds": None},
    )
    assert r.status_code == 403


async def test_ws_invalid_token_closes(server: str) -> None:
    with pytest.raises(websockets.exceptions.WebSocketException):
        async with websockets.connect(ws_url(server, "garbage", "garbage")):
            pass


async def test_ws_token_wrong_project_closes(
    server: str, client: httpx.AsyncClient
) -> None:
    p1 = await create_project(client, "p1", "alice")
    p2 = await create_project(client, "p2", "alice")
    with pytest.raises(websockets.exceptions.WebSocketException):
        async with websockets.connect(
            ws_url(server, p1["member_token"], p2["project_id"])
        ):
            pass


async def test_duplicate_channel_name_rejected(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        await recv_frame(ws, "hello")
        await send(ws, "create_channel", name="general")
        err = await recv_frame(ws, "error")
        assert "already exists" in err["data"]["message"]


@pytest.mark.parametrize("name", ["bad name", "", "with!bang", "tab\tname"])
async def test_invalid_channel_name_rejected(
    server: str, client: httpx.AsyncClient, name: str
) -> None:
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        await recv_frame(ws, "hello")
        await send(ws, "create_channel", name=name)
        err = await recv_frame(ws, "error")
        assert err["data"]["code"] == "bad_request"


async def test_unknown_ws_frame_type_returns_error(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        await recv_frame(ws, "hello")
        await send(ws, "do_a_barrel_roll")
        err = await recv_frame(ws, "error")
        assert err["data"]["code"] == "unknown_type"


async def test_ping_pong(server: str, client: httpx.AsyncClient) -> None:
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        await recv_frame(ws, "hello")
        await send(ws, "ping")
        await recv_frame(ws, "pong")


# ---------------------------------------------------------------------------
# Regression tests for review findings (PR #1)
# ---------------------------------------------------------------------------


async def test_email_address_does_not_trigger_broadcast(
    server: str, client: httpx.AsyncClient
) -> None:
    """`email@here.com` must not fire an @here broadcast (MENTION_RE anchoring)."""
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a, \
               websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b:
        hello = await recv_frame(ws_a, "hello")
        await recv_frame(ws_b, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        # Bob is focused elsewhere so a real @here would deliver a mention frame.
        await send(ws_b, "create_channel", name="other")
        other_id = (await recv_frame(ws_a, "channel_created"))["data"]["channel"]["id"]
        await recv_frame(ws_b, "channel_created")
        await send(ws_b, "mark_read", channel_id=other_id)
        await drain(ws_b)

        await send(ws_a, "send_message", channel_id=general_id, content="email me at alice@here.com")
        msg = await recv_frame(ws_a, "message")
        assert msg["data"]["message"]["mentions"] == []
        await assert_no_frame(ws_b, "mention", within=0.2)


async def test_url_at_does_not_trigger_mention(
    server: str, client: httpx.AsyncClient
) -> None:
    """`https://x.com/@bob` must not @-mention `bob`."""
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a, \
               websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b:
        hello = await recv_frame(ws_a, "hello")
        await recv_frame(ws_b, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws_a, "create_channel", name="other")
        other_id = (await recv_frame(ws_a, "channel_created"))["data"]["channel"]["id"]
        await recv_frame(ws_b, "channel_created")
        await send(ws_b, "mark_read", channel_id=other_id)
        await drain(ws_b)

        await send(ws_a, "send_message", channel_id=general_id, content="see https://x.com/@bob")
        msg = await recv_frame(ws_a, "message")
        assert msg["data"]["message"]["mentions"] == []
        await assert_no_frame(ws_b, "mention", within=0.2)


async def test_author_not_in_broadcast_recipients(
    server: str, client: httpx.AsyncClient
) -> None:
    """The author of an `@channel` broadcast shouldn't get a mention frame."""
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a, \
               websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b:
        hello = await recv_frame(ws_a, "hello")
        await recv_frame(ws_b, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws_a, "create_channel", name="random")
        random_id = (await recv_frame(ws_a, "channel_created"))["data"]["channel"]["id"]
        await recv_frame(ws_b, "channel_created")
        await send(ws_b, "mark_read", channel_id=general_id)
        await drain(ws_b)

        await send(ws_a, "send_message", channel_id=random_id, content="@channel all hands")
        msg = await recv_frame(ws_a, "message")
        assert set(msg["data"]["message"]["mentions"]) == {"bob"}  # alice excluded
        await recv_frame(ws_b, "mention")
        # Alice (the author) must NOT get a mention frame for her own broadcast,
        # regardless of where she's focused.
        await assert_no_frame(ws_a, "mention", within=0.2)


@pytest.mark.parametrize(
    "frame",
    ['[]', '"hi"', '42', 'null', 'true'],
)
async def test_non_dict_frame_does_not_kill_ws(
    server: str, client: httpx.AsyncClient, frame: str
) -> None:
    """Sending JSON that isn't an object returns an error frame and leaves the WS open."""
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        await recv_frame(ws, "hello")
        await ws.send(frame)
        err = await recv_frame(ws, "error")
        assert err["data"]["code"] == "bad_frame"
        # WS still alive: ping/pong round-trips.
        await send(ws, "ping")
        await recv_frame(ws, "pong")


async def test_send_message_with_bad_parent_id_returns_error_frame(
    server: str, client: httpx.AsyncClient
) -> None:
    """Bogus parent_id must surface an error frame, not a disconnect."""
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(
            ws, "send_message", channel_id=general_id, content="hi", parent_id="nope"
        )
        err = await recv_frame(ws, "error")
        assert err["data"]["code"] == "bad_request"
        # Subsequent valid traffic still works.
        await send(ws, "send_message", channel_id=general_id, content="next")
        await recv_frame(ws, "message")


async def test_send_message_parent_id_must_be_same_channel(
    server: str, client: httpx.AsyncClient
) -> None:
    """A parent_id referring to a message in a different channel is rejected."""
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws, "create_channel", name="other")
        other_id = (await recv_frame(ws, "channel_created"))["data"]["channel"]["id"]
        await send(ws, "send_message", channel_id=general_id, content="root")
        root_id = (await recv_frame(ws, "message"))["data"]["message"]["id"]
        await send(
            ws, "send_message", channel_id=other_id, content="reply", parent_id=root_id
        )
        err = await recv_frame(ws, "error")
        assert "different channel" in err["data"]["message"]


async def test_mark_read_with_bad_message_id_returns_error_frame(
    server: str, client: httpx.AsyncClient
) -> None:
    """Bogus message_id must surface an error frame, not a disconnect."""
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws, "mark_read", channel_id=general_id, message_id="nope")
        err = await recv_frame(ws, "error")
        assert err["data"]["code"] == "bad_request"
        # WS still alive.
        await send(ws, "ping")
        await recv_frame(ws, "pong")


async def test_mark_read_with_null_message_id_does_not_wipe_unread(
    server: str, client: httpx.AsyncClient
) -> None:
    """Switching to a channel without history must not advance last_read_at."""
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a:
        hello = await recv_frame(ws_a, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        for content in ["one", "two", "three"]:
            await send(ws_a, "send_message", channel_id=general_id, content=content)
            await recv_frame(ws_a, "message")
    # Bob now sends a focus signal with no message_id; unread should remain 3.
    async with websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b:
        await recv_frame(ws_b, "hello")
        await send(ws_b, "mark_read", channel_id=general_id)
        # The server still responds with an unread_update (no error).
        await recv_frame(ws_b, "unread_update")
    state = await bootstrap(client, bob["member_token"], pid)
    assert state["unread"][general_id]["count"] == 3


async def test_mark_read_anchors_to_message_timestamp(
    server: str, client: httpx.AsyncClient
) -> None:
    """mark_read with a specific message_id anchors last_read_at to that message,
    so a message that arrives after the read (but with an earlier created_at, e.g.
    from another connection mid-flight) still counts as unread."""
    alice = await create_project(client, "demo", "alice")
    bob = await redeem_invite(client, alice["default_invite_token"], "bob")
    pid = alice["project_id"]
    async with websockets.connect(ws_url(server, alice["member_token"], pid)) as ws_a:
        hello = await recv_frame(ws_a, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws_a, "send_message", channel_id=general_id, content="m1")
        m1 = (await recv_frame(ws_a, "message"))["data"]["message"]
        await send(ws_a, "send_message", channel_id=general_id, content="m2")
        await recv_frame(ws_a, "message")
        await send(ws_a, "send_message", channel_id=general_id, content="m3")
        await recv_frame(ws_a, "message")

    # Bob marks read up to m1 only; m2/m3 should still be unread.
    async with websockets.connect(ws_url(server, bob["member_token"], pid)) as ws_b:
        await recv_frame(ws_b, "hello")
        await send(ws_b, "mark_read", channel_id=general_id, message_id=m1["id"])
        upd = await recv_frame(ws_b, "unread_update")
        assert upd["data"]["count"] == 2


async def test_correlation_id_echoed_in_error_response(
    server: str, client: httpx.AsyncClient
) -> None:
    """The optional top-level `id` must be echoed on the matching response."""
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        await recv_frame(ws, "hello")
        await ws.send(json.dumps({"type": "ping", "id": "corr-42"}))
        frame = await recv_frame(ws, "pong")
        assert frame.get("id") == "corr-42"

        await ws.send(json.dumps({"type": "do_a_barrel_roll", "id": "corr-99"}))
        err = await recv_frame(ws, "error")
        assert err.get("id") == "corr-99"


async def test_content_length_cap_returns_error(
    server: str, client: httpx.AsyncClient
) -> None:
    """Messages over the cap surface a clean error frame, not a crash."""
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        huge = "x" * 16000
        await send(ws, "send_message", channel_id=general_id, content=huge)
        err = await recv_frame(ws, "error")
        assert "limit" in err["data"]["message"]


async def test_list_history_bad_limit_returns_error_frame(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        hello = await recv_frame(ws, "hello")
        general_id = hello["data"]["channels"][0]["id"]
        await send(ws, "list_history", channel_id=general_id, limit="abc")
        err = await recv_frame(ws, "error")
        assert err["data"]["code"] == "bad_request"
        # WS still alive.
        await send(ws, "ping")
        await recv_frame(ws, "pong")


@pytest.mark.parametrize("name", ["HERE", "Channel", "HeRe"])
async def test_display_name_reserved_check_is_case_insensitive(
    client: httpx.AsyncClient, name: str
) -> None:
    r = await client.post(
        "/projects", json={"name": "p", "creator_display_name": name}
    )
    assert r.status_code == 400


@pytest.mark.parametrize("name", ["here", "Channel", "HERE"])
async def test_reserved_channel_names_rejected(
    server: str, client: httpx.AsyncClient, name: str
) -> None:
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        await recv_frame(ws, "hello")
        await send(ws, "create_channel", name=name)
        err = await recv_frame(ws, "error")
        assert err["data"]["code"] == "bad_request"


async def test_channel_name_length_cap(
    server: str, client: httpx.AsyncClient
) -> None:
    alice = await create_project(client, "demo", "alice")
    async with websockets.connect(
        ws_url(server, alice["member_token"], alice["project_id"])
    ) as ws:
        await recv_frame(ws, "hello")
        await send(ws, "create_channel", name="x" * 33)
        err = await recv_frame(ws, "error")
        assert err["data"]["code"] == "bad_request"


async def test_invite_max_uses_atomic_under_concurrent_redemption(
    server: str, client: httpx.AsyncClient
) -> None:
    """A max_uses=1 invite must admit exactly one of N concurrent redeemers."""
    alice = await create_project(client, "demo", "alice")
    r = await client.post(
        f"/projects/{alice['project_id']}/invites",
        headers={"Authorization": f"Bearer {alice['member_token']}"},
        json={"max_uses": 1, "expires_in_seconds": None},
    )
    token = r.json()["invite_token"]

    async def attempt(name: str) -> int:
        r = await client.post(
            "/invites/redeem",
            json={"invite_token": token, "display_name": name},
        )
        return r.status_code

    results = await asyncio.gather(
        attempt("alpha"), attempt("bravo"), attempt("charlie"), attempt("delta")
    )
    successes = [s for s in results if s == 200]
    rejections = [s for s in results if s == 400]
    assert len(successes) == 1, f"expected exactly 1 successful redemption, got {results}"
    assert len(rejections) == 3
