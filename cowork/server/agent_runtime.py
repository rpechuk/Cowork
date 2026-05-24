"""Agent runtime abstraction.

The server invokes one of these to actually drive a Claude agent. The default
implementation wraps `claude-agent-sdk`; tests substitute MockAgentRuntime so
no real API calls are made.

Selection is via the COWORK_AGENT_RUNTIME env var:
  - unset / "claude" : use the real claude-agent-sdk runtime
  - "mock"           : MockAgentRuntime (echoes a canned reply)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

logger = logging.getLogger("cowork.agent_runtime")


@dataclass
class AgentInvocation:
    agent_display_name: str
    system_prompt: str
    model: Optional[str]
    cwd: Path
    api_key: str
    # The serialized recent conversation that the agent should treat as the
    # transcript leading up to its turn. Each item is `<display_name>: <text>`
    # so the agent sees who said what.
    transcript: list[str]
    # The agent's instruction for this turn — usually "respond now" or the
    # specific message that triggered it.
    user_prompt: str


class AgentRuntime(Protocol):
    async def run(self, invocation: AgentInvocation) -> str:
        """Run the agent to completion and return the text reply.

        Returning an empty string means the agent declined to respond.
        Raises on transport / authentication failures.
        """


class MockAgentRuntime:
    """Test runtime. Returns a deterministic reply derived from the prompt."""

    def __init__(self, reply: str | None = None) -> None:
        self.reply = reply
        self.calls: list[AgentInvocation] = []

    async def run(self, invocation: AgentInvocation) -> str:
        self.calls.append(invocation)
        if self.reply is not None:
            return self.reply
        # Default: echo the trigger text prefixed with the agent's name.
        return f"[{invocation.agent_display_name} replying] " + invocation.user_prompt[:120]


class ClaudeAgentRuntime:
    """Wraps claude-agent-sdk.query() with our AgentInvocation contract."""

    async def run(self, invocation: AgentInvocation) -> str:
        # Imported lazily so test runs without the optional dep also work.
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )

        env = dict(os.environ)
        env["ANTHROPIC_API_KEY"] = invocation.api_key
        invocation.cwd.mkdir(parents=True, exist_ok=True)
        prompt_parts = []
        if invocation.transcript:
            prompt_parts.append("Recent channel transcript:")
            prompt_parts.extend(invocation.transcript)
            prompt_parts.append("")
        prompt_parts.append(invocation.user_prompt)
        prompt = "\n".join(prompt_parts)

        options = ClaudeAgentOptions(
            system_prompt=invocation.system_prompt or None,
            model=invocation.model,
            cwd=str(invocation.cwd),
            env=env,
            # Default permission mode: allow tools but block destructive ones.
            # Operators who want stricter behavior can wrap the runtime.
            permission_mode="default",
        )
        chunks: list[str] = []
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
                elif isinstance(message, ResultMessage):
                    # Conversation done.
                    break
        except Exception:
            logger.exception("claude-agent-sdk query failed")
            raise
        return "".join(chunks).strip()


def runtime_from_env() -> AgentRuntime:
    name = os.environ.get("COWORK_AGENT_RUNTIME", "claude").lower()
    if name == "mock":
        return MockAgentRuntime()
    return ClaudeAgentRuntime()


# Test hook: setting `_OVERRIDE_RUNTIME` swaps the global runtime in.
_OVERRIDE_RUNTIME: Optional[AgentRuntime] = None
_RUNTIME_LOCK = asyncio.Lock()


def set_runtime_override(runtime: Optional[AgentRuntime]) -> None:
    global _OVERRIDE_RUNTIME
    _OVERRIDE_RUNTIME = runtime


def get_runtime() -> AgentRuntime:
    return _OVERRIDE_RUNTIME or runtime_from_env()
