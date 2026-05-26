"""Server-side adapter that runs an agent turn against the Claude Agent SDK.

The class hierarchy is deliberately tiny so tests can swap in a mock that
returns canned responses without ever loading the SDK or hitting the
network — see `FakeAgentRunner` below.

A single `AgentRunner` instance is attached to `app.state.agent_runner` in
the FastAPI lifespan, defaulting to `ClaudeSDKAgentRunner`. Tests overwrite
that attribute after the server starts to inject a fake."""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Protocol

from cowork.shared.protocol import AgentConfig, Message

logger = logging.getLogger("cowork.agent_runner")


class AgentRunner(Protocol):
    """Minimal interface the WS message handler depends on. Implementations
    are responsible for whatever transport, retry, and error handling the
    underlying provider needs."""

    async def respond(
        self,
        config: AgentConfig,
        agent_display_name: str,
        history: list[Message],
        invoker_display_name: str,
    ) -> str:
        ...


def _format_transcript(
    history: list[Message],
    agent_display_name: str,
) -> str:
    """Render the recent channel transcript as a plain chat log. Each line
    is `@speaker: text`; the agent reads its own past turns under its own
    name so it sees the conversation the way humans see it."""
    lines = []
    for msg in history:
        speaker = msg.display_name or "?"
        lines.append(f"@{speaker}: {msg.content}")
    return "\n".join(lines)


def _build_prompt(
    history: list[Message],
    agent_display_name: str,
    invoker_display_name: str,
) -> str:
    """Bake the transcript + the explicit ask into one user-turn prompt for
    the SDK. The agent's system prompt lives in ClaudeAgentOptions, so this
    string only needs the contextual material."""
    transcript = _format_transcript(history, agent_display_name)
    return (
        f"You are @{agent_display_name}, participating in a group chat.\n"
        f"@{invoker_display_name} just addressed you. Reply with your"
        f" response only — do NOT prefix it with your own name, do NOT"
        f" quote them; the chat layer attributes your message automatically.\n"
        "\n"
        "Recent transcript:\n"
        f"{transcript}\n"
    )


class ClaudeSDKAgentRunner:
    """Default runner — backed by `claude_agent_sdk.query`. Importing the
    SDK is lazy so the rest of the server (and the test suite) doesn't pay
    the import cost or require the CLI binary to be present."""

    async def respond(
        self,
        config: AgentConfig,
        agent_display_name: str,
        history: list[Message],
        invoker_display_name: str,
    ) -> str:
        try:
            from claude_agent_sdk import (  # noqa: PLC0415 — lazy by design
                ClaudeAgentOptions,
                query,
            )
        except ImportError as e:
            raise RuntimeError(
                "claude_agent_sdk is not installed. `pip install"
                " claude-agent-sdk` to enable agent responses."
            ) from e

        prompt = _build_prompt(
            history[-config.history_messages :],
            agent_display_name,
            invoker_display_name,
        )
        options = ClaudeAgentOptions(
            system_prompt=config.system_prompt,
            model=config.model,
            # No tools by default — these agents are pure chat
            # participants. Future iterations can opt them in.
            allowed_tools=[],
        )
        chunks: list[str] = []
        try:
            async for message in query(prompt=prompt, options=options):
                chunks.extend(_collect_text(message))
        except Exception:
            logger.exception(
                "claude SDK query failed for agent=%s", agent_display_name
            )
            raise
        return "".join(chunks).strip() or "(no response)"


def _collect_text(message: object) -> Iterable[str]:
    """Pull plain text out of any SDK message. The SDK emits AssistantMessage
    and other kinds; only assistant text blocks belong in the channel."""
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return ()
    out = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            out.append(text)
    return out


class FakeAgentRunner:
    """Test-only runner that records every invocation and returns canned
    responses round-robin. Use as `app.state.agent_runner = FakeAgentRunner(...)`
    after the server has started."""

    def __init__(self, responses: Optional[list[str]] = None) -> None:
        self.responses = responses or ["(mock reply)"]
        self.calls: list[dict] = []

    async def respond(
        self,
        config: AgentConfig,
        agent_display_name: str,
        history: list[Message],
        invoker_display_name: str,
    ) -> str:
        self.calls.append(
            {
                "agent": agent_display_name,
                "invoker": invoker_display_name,
                "history": [m.content for m in history],
                "system_prompt": config.system_prompt,
                "model": config.model,
            }
        )
        return self.responses[(len(self.calls) - 1) % len(self.responses)]
