"""Server-side adapter that runs an agent turn against the Claude Agent SDK.

The class hierarchy is deliberately tiny so tests can swap in a mock that
returns canned responses without ever loading the SDK or hitting the
network — see `FakeAgentRunner` below.

A single `AgentRunner` instance is attached to `app.state.agent_runner` in
the FastAPI lifespan, defaulting to `ClaudeSDKAgentRunner`. Tests overwrite
that attribute after the server starts to inject a fake."""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Iterable, Iterator, Optional, Protocol

from cowork.shared.protocol import AgentConfig, Message

logger = logging.getLogger("cowork.agent_runner")


# Progress event "kind" values broadcast to clients in agent_progress
# frames. Keep this list short and stable — the TUI renders each kind
# slightly differently (thinking is dimmed, tool_use is highlighted).
ProgressKind = str  # "thinking" | "text" | "tool_use" | "tool_result"

# Async progress callback: (kind, text) -> awaitable. The runner awaits
# it for every event it wants to stream out. Returning fast matters
# (each await blocks the SDK iteration loop).
ProgressCallback = Callable[[ProgressKind, str], Awaitable[None]]


class AgentRunner(Protocol):
    """Minimal interface the WS message handler depends on. Implementations
    are responsible for whatever transport, retry, and error handling the
    underlying provider needs.

    `on_progress`, when supplied, is called with intermediate events as
    the agent works (streamed text, thinking blocks, tool invocations).
    Implementations that can't surface intermediate state may ignore it."""

    async def respond(
        self,
        config: AgentConfig,
        agent_display_name: str,
        history: list[Message],
        invoker_display_name: str,
        on_progress: Optional[ProgressCallback] = None,
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


def _iter_progress_events(message: object) -> Iterator[tuple[str, str]]:
    """Walk one SDK message and yield (kind, text) progress events for
    every block we want to surface to clients.

    The SDK emits AssistantMessage / UserMessage / SystemMessage /
    ResultMessage. We care about the assistant's content blocks:
      - ThinkingBlock → ('thinking', <text>) — extended-thinking step
      - TextBlock     → ('text', <text>)     — partial response chunk
      - ToolUseBlock  → ('tool_use', '<name>: <args summary>')
    """
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return
    for block in content:
        cls_name = type(block).__name__
        # Thinking blocks come BEFORE text in extended-thinking; surface
        # them so users see the reasoning trail, not just the final answer.
        if cls_name == "ThinkingBlock":
            text = getattr(block, "thinking", None) or getattr(block, "text", None)
            if isinstance(text, str) and text.strip():
                yield ("thinking", text)
            continue
        if cls_name == "TextBlock":
            text = getattr(block, "text", None)
            if isinstance(text, str) and text.strip():
                yield ("text", text)
            continue
        # Tool use / server tool use. Both expose a tool name + input
        # dict; we collapse the input to a tight one-liner so the TUI
        # doesn't have to render arbitrary nested JSON.
        if cls_name in ("ToolUseBlock", "ServerToolUseBlock"):
            tool_name = getattr(block, "name", "?")
            tool_input = getattr(block, "input", None)
            if isinstance(tool_input, dict) and tool_input:
                # Best-effort short summary; truncate to keep the wire
                # frame small.
                summary = ", ".join(f"{k}={v!r}" for k, v in tool_input.items())
                if len(summary) > 200:
                    summary = summary[:197] + "..."
                yield ("tool_use", f"{tool_name}({summary})")
            else:
                yield ("tool_use", f"{tool_name}()")


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
        on_progress: Optional[ProgressCallback] = None,
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
                if on_progress is not None:
                    for kind, text in _iter_progress_events(message):
                        # Swallow per-event errors so one slow / dead
                        # client can't tank the whole SDK call.
                        try:
                            await on_progress(kind, text)
                        except Exception:
                            logger.exception(
                                "on_progress callback failed for"
                                " agent=%s",
                                agent_display_name,
                            )
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
    after the server has started.

    Optional `progress` argument: a list of (kind, text) tuples that get
    fed through the `on_progress` callback before the final response is
    returned. Lets tests assert the full streaming path works without
    actually running the SDK."""

    def __init__(
        self,
        responses: Optional[list[str]] = None,
        progress: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        self.responses = responses or ["(mock reply)"]
        self.progress = progress or []
        self.calls: list[dict] = []

    async def respond(
        self,
        config: AgentConfig,
        agent_display_name: str,
        history: list[Message],
        invoker_display_name: str,
        on_progress: Optional[ProgressCallback] = None,
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
        if on_progress is not None:
            for kind, text in self.progress:
                await on_progress(kind, text)
        return self.responses[(len(self.calls) - 1) % len(self.responses)]
