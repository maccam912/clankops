from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage

from debug_auth import print_exception_debug
from memory_store import MemoryStore


RATE_LIMIT_WAIT_SECONDS = 60
RATE_LIMIT_RECOVERY_INSTRUCTION = (
    "We just hit a provider rate limit (HTTP 429). After this retry, respond as concisely as possible. "
    "If you cannot fully complete the task, it's OK to give a partial answer and stop. "
    "Avoid extra tool calls unless absolutely necessary."
)


def _is_rate_limit_error(err: Exception) -> bool:
    status_code = getattr(err, "status_code", None)
    if status_code == 429:
        return True

    body = getattr(err, "body", None)
    if isinstance(body, dict):
        if body.get("code") == 429:
            return True
        nested = body.get("error")
        if isinstance(nested, dict) and nested.get("code") == 429:
            return True

    msg = str(err).lower()
    return "rate limit" in msg or "429" in msg


def _rate_limit_detail(err: Exception) -> str:
    model_name = getattr(err, "model_name", None)
    body = getattr(err, "body", None)

    message: str | None = None
    if isinstance(body, dict):
        raw = body.get("message")
        if isinstance(raw, str) and raw.strip():
            message = raw.strip()

    if not message:
        message = str(err).strip()

    model_part = f" model={model_name}" if model_name else ""
    return f"{message}{model_part}"


@dataclass
class SessionContext:
    """Shared mutable state passed as deps to all agents.

    Tools can read this to access session information and write
    to it to request state transitions.
    """

    # Simplified event log for building context prompts
    events: list[dict[str, str]] = field(default_factory=list)

    # Journal entries accumulated over the session
    journal_entries: list[str] = field(default_factory=list)

    # Persistent memory blocks loaded from disk
    memory_store: MemoryStore = field(default_factory=MemoryStore, repr=False)
    identity_block: str = field(default="", repr=False)
    human_block: str = field(default="", repr=False)

    # Transition signaling - tools set these to request a state change
    _transition_target: str | None = field(default=None, repr=False)
    _transition_reason: str = field(default="", repr=False)

    def __post_init__(self):
        self.identity_block = self.memory_store.load_identity()
        self.human_block = self.memory_store.load_human()
        self.journal_entries = self.memory_store.load_journal_entries()

    def request_transition(self, target: str, reason: str = ""):
        """Called by agent tools to request a state transition."""
        self._transition_target = target
        self._transition_reason = reason

    def consume_transition(self) -> tuple[str, str] | None:
        """Check for and clear any pending transition request.

        Returns (target, reason) if a transition was requested, else None.
        """
        if self._transition_target is not None:
            target = self._transition_target
            reason = self._transition_reason
            self._transition_target = None
            self._transition_reason = ""
            return target, reason
        return None

    def log_event(self, kind: str, content: str, state: str = ""):
        """Record an event in the session log."""
        self.events.append({"kind": kind, "content": content, "state": state})

    def set_identity_block(self, value: str):
        text = value.strip()
        if not text:
            return
        self.identity_block = text
        try:
            self.memory_store.save_identity(text)
        except Exception as err:
            self.log_event("warning", f"Failed to save identity block: {err}")

    def set_human_block(self, value: str):
        text = value.strip()
        if not text:
            return
        self.human_block = text
        try:
            self.memory_store.save_human(text)
        except Exception as err:
            self.log_event("warning", f"Failed to save human block: {err}")

    def add_journal_entry(self, value: str):
        text = value.strip()
        if not text:
            return
        self.journal_entries.append(text)
        try:
            self.memory_store.append_journal_entry(text)
        except Exception as err:
            self.log_event("warning", f"Failed to append journal entry: {err}")

    def memory_blocks_prompt(self) -> str:
        return (
            "MEMORY BLOCKS\n"
            "IDENTITY:\n"
            f"{self.identity_block}\n\n"
            "HUMAN:\n"
            f"{self.human_block}"
        )

    def recent_conversation(self, n: int = 10) -> str:
        """Get a text summary of the last n conversation events."""
        conv_events = [e for e in self.events if e["kind"] in ("user", "assistant")]
        recent = conv_events[-n:]
        if not recent:
            return "(No conversation yet.)"
        lines = []
        for e in recent:
            role = "User" if e["kind"] == "user" else "Assistant"
            lines.append(f"{role}: {e['content']}")
        return "\n".join(lines)


@dataclass
class StateConfig:
    """Configuration for a single state in the agent state machine."""

    name: str
    agent: Agent[SessionContext, str]

    # Idle timeout: transition to idle_target after this many seconds without input
    idle_timeout: float | None = None
    idle_target: str | None = None

    # After an autonomous run (triggered by timer), return to this state
    auto_return: str | None = None

    # If True, clear this state's message history and the event log on entry
    # (journal entries are always preserved)
    clear_on_enter: bool = False

    # If True, run this state once immediately after transition.
    # Used for autonomous background states like journaling/memory updates.
    auto_run_on_enter: bool = False


class StateMachine:
    """Orchestrates agent states, transitions, and the async event loop."""

    def __init__(
        self,
        initial_state: str,
        input_reader: Callable[[], Awaitable[str]] | None = None,
        response_sink: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.states: dict[str, StateConfig] = {}
        self.context = SessionContext()
        self.current_state_name = initial_state
        # Per-state conversation history for pydantic-ai message continuity
        self._histories: dict[str, list[ModelMessage]] = {}
        self._pending_auto_run = False
        self._input_reader = input_reader or self._read_stdin
        self._response_sink = response_sink

    def add_state(self, config: StateConfig):
        self.states[config.name] = config
        self._histories[config.name] = []

    @property
    def current(self) -> StateConfig:
        return self.states[self.current_state_name]

    def transition_to(self, target: str, reason: str = "") -> bool:
        if target not in self.states:
            print(f"[machine] Unknown state '{target}', ignoring.")
            return False
        old = self.current_state_name
        self.current_state_name = target
        self.context.log_event("transition", f"{old} -> {target}: {reason}")
        detail = f" ({reason})" if reason else ""
        print(f"\n[{old} -> {target}]{detail}")

        target_state = self.states[target]
        if target_state.clear_on_enter:
            self._histories[target] = []
            self.context.events.clear()

        self._pending_auto_run = target_state.auto_run_on_enter

        return True

    async def run(self):
        """Main event loop."""
        print(f"[machine] Starting in '{self.current_state_name}' state")
        state = self.current
        if state.idle_timeout:
            print(f"[machine] Idle timeout: {state.idle_timeout}s -> '{state.idle_target}'")
        print()

        input_task = asyncio.create_task(self._input_reader())

        while True:
            state = self.current

            if self._pending_auto_run:
                self._pending_auto_run = False
                await self._run_agent_autonomous()
                continue

            # Set up idle timer if configured for this state
            # BUT only if we've had at least one interaction (so we don't journal empty air at startup)
            timer_task = None
            has_interaction = any(e["kind"] in ("user", "assistant") for e in self.context.events)
            if state.idle_timeout and state.idle_target and has_interaction:
                timer_task = asyncio.create_task(asyncio.sleep(state.idle_timeout))

            wait_for: list[asyncio.Task] = [input_task]
            if timer_task:
                wait_for.append(timer_task)

            done, _ = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)

            if input_task in done:
                raw = input_task.result()
                if not raw:
                    # EOF (e.g. Ctrl+D / Ctrl+Z)
                    print("\n[machine] Input closed.")
                    break
                user_input = raw.strip()
                if timer_task:
                    timer_task.cancel()
                input_task = asyncio.create_task(self._input_reader())

                if user_input:
                    self.context.log_event("user", user_input, state.name)
                    await self._run_agent(state, user_input)

            elif timer_task and timer_task in done:
                # Idle timeout fired
                self.transition_to(state.idle_target, f"idle for {state.idle_timeout}s")
                continue

    async def _run_agent(self, state: StateConfig, user_input: str):
        """Run the agent for the given state with user input."""
        prompt = f"{self.context.memory_blocks_prompt()}\n\nUSER INPUT:\n{user_input}"
        response = await self._run_agent_with_rate_limit_retry(
            state,
            prompt,
            message_history=self._histories[state.name],
        )
        if response is None:
            return

        self.context.log_event("assistant", response, state.name)
        print(f"[{state.name}]: {response}")
        if self._response_sink is not None:
            try:
                await self._response_sink(response)
            except Exception as sink_err:
                print(f"[machine] Response sink error: {sink_err}")

        self._check_transition()

    async def _run_agent_autonomous(self):
        """Run the current state's agent without user input (e.g. journaling on idle)."""
        state = self.current

        # Build a context-aware prompt for autonomous invocation
        recent = self.context.recent_conversation()
        prompt = (
            f"{self.context.memory_blocks_prompt()}\n\n"
            f"Recent conversation:\n{recent}\n\n"
            "Please proceed with your task."
        )

        response = await self._run_agent_with_rate_limit_retry(
            state,
            prompt,
            message_history=self._histories.get(state.name, []),
        )
        if response is not None:
            self.context.log_event("assistant", response, state.name)
            print(f"[{state.name}]: {response}")
            if self._response_sink is not None:
                try:
                    await self._response_sink(response)
                except Exception as sink_err:
                    print(f"[machine] Response sink error: {sink_err}")

        # Check for tool-requested transition first
        if not self._check_transition():
            # Auto-return if configured and no explicit transition was requested
            if state.auto_return:
                self.transition_to(state.auto_return, "task complete")

    async def _run_agent_with_rate_limit_retry(
        self,
        state: StateConfig,
        prompt: str,
        message_history: list[ModelMessage],
    ) -> str | None:
        """Run agent once, and retry once on rate limit after a fixed wait."""
        try:
            result = await state.agent.run(
                prompt,
                deps=self.context,
                message_history=message_history,
            )
            self._histories[state.name] = result.all_messages()
            return result.output
        except Exception as err:
            if not _is_rate_limit_error(err):
                print(f"[{state.name}] Error: {err}")
                print_exception_debug(state.name, err)
                return None

            detail = _rate_limit_detail(err)
            msg = (
                f"[{state.name}] Rate limit encountered (429). Waiting {RATE_LIMIT_WAIT_SECONDS}s then retrying. "
                f"({detail})"
            )
            print(msg)
            if self._response_sink is not None:
                try:
                    await self._response_sink(msg)
                except Exception as sink_err:
                    print(f"[machine] Response sink error: {sink_err}")

            await asyncio.sleep(RATE_LIMIT_WAIT_SECONDS)

            retry_prompt = f"{prompt}\n\nRATE LIMIT RECOVERY:\n{RATE_LIMIT_RECOVERY_INSTRUCTION}"
            try:
                result = await state.agent.run(
                    retry_prompt,
                    deps=self.context,
                    message_history=message_history,
                )
                self._histories[state.name] = result.all_messages()
                return result.output
            except Exception as retry_err:
                print(f"[{state.name}] Error after rate-limit wait: {retry_err}")
                print_exception_debug(state.name, retry_err)
                return None

    def _check_transition(self) -> bool:
        """Check if any tool requested a transition. Returns True if one occurred."""
        transition = self.context.consume_transition()
        if transition:
            target, reason = transition
            return self.transition_to(target, reason)
        return False

    @staticmethod
    async def _read_stdin() -> str:
        return await asyncio.to_thread(sys.stdin.readline)
