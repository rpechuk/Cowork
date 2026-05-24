"""Agent dispatch and loop guard.

Sits between `_handle_send_message` and the actual claude-agent-sdk invocation:
  - decides which agents in a channel should respond to a new message,
  - enforces a per-channel consecutive-agent-message cap so two agents can't
    ping-pong forever,
  - serializes invocations per agent (an agent never has two replies in flight
    at once),
  - holds per-member API keys in memory (never on disk).

Per-member API keys are bound to the lifetime of the member's active WS
session(s). When the last session for a member disconnects, their keys are
cleared and any agents they own become dormant until the next session.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable, Optional

from cowork.shared.protocol import Agent, Message

from cowork.server.agent_runtime import AgentInvocation, get_runtime
from cowork.server.db import Database, MENTION_RE

logger = logging.getLogger("cowork.agents")

DEFAULT_LOOP_GUARD = 4
TRANSCRIPT_TAIL = 30  # how many recent messages to feed the agent
QUESTION_RE = re.compile(r"\?\s*$")

# Type for the "post on behalf of an agent" callback the manager calls
# after a successful run. Provided by app.py so the new agent message goes
# through the same broadcast path as a human message.
PostAsAgentFn = Callable[[str, str, str], Awaitable[None]]  # (channel_id, agent_member_id, content)


class AgentManager:
    def __init__(
        self,
        db: Database,
        workspace_root: Path,
        post_as_agent: PostAsAgentFn,
        loop_guard: int = DEFAULT_LOOP_GUARD,
    ) -> None:
        self.db = db
        self.workspace_root = workspace_root
        self.post_as_agent = post_as_agent
        self.loop_guard = loop_guard
        # member_id -> api_key (in-memory only; cleared on last WS disconnect)
        self._api_keys: dict[str, str] = {}
        # member_id -> set of WS connection ids that currently hold this key
        self._key_holders: dict[str, set[int]] = {}
        # channel_id -> consecutive agent messages since the last human turn
        self._streak: dict[str, int] = {}
        # member_id -> Lock to serialize that agent's runs
        self._agent_locks: dict[str, asyncio.Lock] = {}
        # Pending tasks so we can cancel them on shutdown.
        self._tasks: set[asyncio.Task] = set()

    # ---- API key registry ----

    def register_api_key(self, member_id: str, connection_id: int, api_key: str) -> None:
        self._api_keys[member_id] = api_key
        self._key_holders.setdefault(member_id, set()).add(connection_id)

    def release_connection(self, member_id: str, connection_id: int) -> None:
        holders = self._key_holders.get(member_id)
        if not holders:
            return
        holders.discard(connection_id)
        if not holders:
            self._key_holders.pop(member_id, None)
            self._api_keys.pop(member_id, None)

    def api_key_for(self, owner_member_id: str) -> Optional[str]:
        return self._api_keys.get(owner_member_id)

    # ---- dispatch ----

    def reset_streak(self, channel_id: str) -> None:
        self._streak[channel_id] = 0

    def streak(self, channel_id: str) -> int:
        return self._streak.get(channel_id, 0)

    async def on_message(self, message: Message) -> None:
        """Called after every successful post_message. Schedules agent
        responses asynchronously so the original send path returns quickly.
        """
        author = await self.db.get_member(message.member_id)
        if author is None:
            return
        if author.is_agent:
            self._streak[message.channel_id] = self._streak.get(message.channel_id, 0) + 1
        else:
            self._streak[message.channel_id] = 0

        if self._streak.get(message.channel_id, 0) >= self.loop_guard:
            logger.info(
                "Loop guard tripped on channel %s (streak=%d); agents will not respond"
                " until a human posts.",
                message.channel_id,
                self._streak[message.channel_id],
            )
            return

        channel = await self.db.get_channel(message.channel_id)
        if channel is None:
            return
        candidates = await self.db.list_agents_for_channel(
            channel.project_id, channel.id
        )
        for agent in candidates:
            if agent.member_id == message.member_id:
                continue  # don't re-trigger an agent on its own message
            if not self._should_respond(agent, message):
                continue
            api_key = self._api_keys.get(agent.owner_member_id)
            if not api_key:
                logger.debug(
                    "Skipping agent %s: owner %s has no live API key registered",
                    agent.display_name,
                    agent.owner_member_id,
                )
                continue
            task = asyncio.create_task(self._invoke(agent, message, channel.id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _should_respond(self, agent: Agent, message: Message) -> bool:
        if agent.trigger_mode == "always":
            return True
        if agent.trigger_mode == "on_question":
            return bool(QUESTION_RE.search(message.content))
        # on_mention: case-insensitive match on the agent's display name,
        # using the same boundary rules as MENTION_RE.
        names = {m.group(1).lower() for m in MENTION_RE.finditer(message.content)}
        return agent.display_name.lower() in names

    async def _invoke(self, agent: Agent, trigger: Message, channel_id: str) -> None:
        lock = self._agent_locks.setdefault(agent.member_id, asyncio.Lock())
        async with lock:
            api_key = self._api_keys.get(agent.owner_member_id)
            if not api_key:
                return
            history = await self.db.history(channel_id, None, TRANSCRIPT_TAIL)
            transcript = [
                f"@{m.display_name}: {m.content}" for m in history if m.id != trigger.id
            ]
            invocation = AgentInvocation(
                agent_display_name=agent.display_name,
                system_prompt=self._compose_system_prompt(agent, channel_id),
                model=agent.model,
                cwd=self._workspace_for(channel_id),
                api_key=api_key,
                transcript=transcript,
                user_prompt=(
                    f"@{trigger.display_name} just said: {trigger.content}\n\n"
                    "Respond as your character. Keep replies concise unless the user"
                    " has asked for depth. If you have nothing useful to add, reply"
                    " with an empty string."
                ),
            )
            try:
                reply = await get_runtime().run(invocation)
            except Exception:
                logger.exception("Agent %s failed during run", agent.display_name)
                return
            reply = (reply or "").strip()
            if not reply:
                return
            try:
                await self.post_as_agent(channel_id, agent.member_id, reply)
            except Exception:
                logger.exception("Failed to post agent reply for %s", agent.display_name)

    def _compose_system_prompt(self, agent: Agent, channel_id: str) -> str:
        base = agent.system_prompt or (
            f"You are {agent.display_name}, an AI participant in a multi-person chat."
        )
        return (
            f"{base}\n\n"
            f"You are speaking in a Cowork channel. Your display name is "
            f"@{agent.display_name}. Other participants may be humans or other agents."
            f" When you address someone, prefix their name with @ (e.g. @alice). Stay"
            f" concise; multi-paragraph replies are jarring in a chat."
        )

    def _workspace_for(self, channel_id: str) -> Path:
        d = self.workspace_root / channel_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
