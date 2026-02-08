from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import timezone, datetime
import os
from functools import lru_cache
from typing import Awaitable, Callable

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
import tiktoken

from debug_auth import print_exception_debug
from memory_store import MemoryStore


RATE_LIMIT_WAIT_SECONDS = 60

def _extract_http_status(err: Exception) -> int | None:
    status_code = getattr(err, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    response = getattr(err, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int):
        return response_status

    return None


def _llm_should_cooldown_and_retry(err: Exception) -> tuple[bool, int | None]:
    """Return (should_retry, status_code_bucket).

    `status_code_bucket` is either 429, 401, or None when unknown.
    Treat 401 like 429 for cooldown/retry purposes (per repo config).
    """
    status_code = _extract_http_status(err)
    if status_code in (429, 401):
        return True, status_code

    body = getattr(err, "body", None)
    if isinstance(body, dict):
        code = body.get("code")
        if code == 429:
            return True, 429
        nested = body.get("error")
        if isinstance(nested, dict) and nested.get("code") == 429:
            return True, 429

    msg = str(err).lower()
    if "rate limit" in msg or "429" in msg:
        return True, 429
    if "unauthorized" in msg or " 401" in msg or "401 " in msg:
        return True, 401

    return False, None


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


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except Exception:
        return default


@dataclass(frozen=True)
class InboundMessage:
    user_id: int
    text: str
    chat_id: int | None = None


@lru_cache(maxsize=1)
def _token_encoder():
    # Prefer the 200k encoding (best fit for modern large-context models), fall back if unavailable.
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _token_encoder()
    return len(enc.encode(text, disallowed_special=()))


def _truncate_text_middle_by_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to <= max_tokens, keeping the start and end with a marker in the middle."""
    if max_tokens <= 0:
        return ""
    if not text:
        return text

    enc = _token_encoder()
    toks = enc.encode(text, disallowed_special=())
    if len(toks) <= max_tokens:
        return text

    # Keep some head to preserve instructions/context labels, and tail to keep most recent details.
    head = min(256, max_tokens // 4)
    tail = max(0, max_tokens - head)
    head_toks = toks[:head]
    tail_toks = toks[-tail:] if tail else []
    marker = "\n\n[...context truncated to fit model context window...]\n\n"
    combined = enc.decode(head_toks) + marker + enc.decode(tail_toks)

    # Paranoia: ensure we didn't exceed due to marker expansion.
    while _count_tokens(combined) > max_tokens and head > 0:
        head = max(0, head - 16)
        head_toks = toks[:head]
        combined = enc.decode(head_toks) + marker + enc.decode(tail_toks)
    while _count_tokens(combined) > max_tokens and tail > 0:
        tail = max(0, tail - 16)
        tail_toks = toks[-tail:] if tail else []
        combined = enc.decode(head_toks) + marker + enc.decode(tail_toks)

    return combined


def _model_message_to_text(msg: ModelMessage) -> str:
    """Convert pydantic-ai messages to plain-ish text so we can estimate tokens."""
    kind = getattr(msg, "kind", None)
    parts = getattr(msg, "parts", None)
    if not parts:
        return str(msg)

    chunks: list[str] = []
    if kind == "request":
        instructions = getattr(msg, "instructions", None)
        if isinstance(instructions, str) and instructions.strip():
            chunks.append(instructions.strip())
    for p in parts:
        content = getattr(p, "content", None)
        if isinstance(content, str):
            chunks.append(content)
            continue
        if content is not None:
            chunks.append(str(content))
            continue
        tool_name = getattr(p, "tool_name", None)
        if isinstance(tool_name, str) and tool_name.strip():
            chunks.append(f"tool:{tool_name}")
        args = getattr(p, "args", None)
        if args is not None:
            chunks.append(str(args))
    return "\n".join(chunks)


def _fit_prompt_and_history_to_context(
    *,
    prompt: str,
    message_history: list[ModelMessage],
    max_context_tokens: int,
    max_completion_tokens: int,
    margin_tokens: int,
) -> tuple[str, list[ModelMessage], int]:
    """Ensure (history + prompt + completion + margin) fits in the model context window."""
    max_context_tokens = max(4096, int(max_context_tokens))
    margin_tokens = max(0, int(margin_tokens))
    max_completion_tokens = max(256, int(max_completion_tokens))

    if max_completion_tokens + margin_tokens > max_context_tokens:
        max_completion_tokens = max(256, max_context_tokens - margin_tokens)

    budget = max_context_tokens - max_completion_tokens - margin_tokens
    budget = max(512, budget)

    hist = list(message_history or [])

    def total_tokens(h: list[ModelMessage], p: str) -> int:
        t = _count_tokens(p)
        for m in h:
            t += _count_tokens(_model_message_to_text(m))
        return t

    while hist and total_tokens(hist, prompt) > budget:
        hist.pop(0)

    if total_tokens(hist, prompt) > budget:
        prompt = _truncate_text_middle_by_tokens(prompt, budget)

    return prompt, hist, max_completion_tokens


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
    active_user_id: int = field(default=0, repr=False)
    main_user_id: int = field(default=0, repr=False)
    _human_cache: dict[int, str] = field(default_factory=dict, init=False, repr=False)

    # Transition signaling - tools set these to request a state change
    _transition_target: str | None = field(default=None, repr=False)
    _transition_reason: str = field(default="", repr=False)

    def __post_init__(self):
        # In telegram mode, TELEGRAM_USER_ID is the main user. In non-telegram mode, this stays 0.
        if not self.main_user_id:
            raw = os.environ.get("TELEGRAM_USER_ID", "").strip()
            try:
                self.main_user_id = int(raw) if raw else 0
            except Exception:
                self.main_user_id = 0

        self.identity_block = self.memory_store.load_identity()
        self.journal_entries = self.memory_store.load_journal_entries()
        # Default active user is main user when available; otherwise 0.
        self.set_active_user(self.main_user_id or 0)

    def set_active_user(self, user_id: int) -> None:
        uid = int(user_id or 0)
        self.active_user_id = uid
        if uid in self._human_cache:
            self.human_block = self._human_cache[uid]
            return
        text = self.memory_store.load_human(user_id=uid)
        self._human_cache[uid] = text
        self.human_block = text

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
        self._human_cache[int(self.active_user_id or 0)] = text
        try:
            self.memory_store.save_human(user_id=int(self.active_user_id or 0), content=text)
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

    def memory_blocks_prompt(self, recent_journal_entries: int = 2) -> str:
        n = max(0, min(int(recent_journal_entries), 20))
        recent = self.journal_entries[-n:] if n and self.journal_entries else []
        if recent:
            journal_lines = [f"{i}. {entry}" for i, entry in enumerate(recent, 1)]
            journal_block = "\n".join(journal_lines)
        else:
            journal_block = "(No journal entries yet.)"

        return (
            "MEMORY BLOCKS\n"
            "IDENTITY:\n"
            f"{self.identity_block}\n\n"
            f"HUMAN (user_id={int(self.active_user_id or 0)}):\n"
            f"{self.human_block}\n\n"
            "RECENT JOURNAL ENTRIES:\n"
            f"{journal_block}"
        )

    def recent_conversation(self, *, user_id: int, n: int = 12, since_seconds: int | None = None) -> str:
        """Get a text rendering of the last n chat messages for a given user."""
        uid = int(user_id or 0)
        since_utc: int | None = None
        if since_seconds is not None:
            now = int(datetime.now(tz=timezone.utc).timestamp())
            since_utc = max(0, now - max(0, int(since_seconds)))
        msgs = self.memory_store.load_recent_chat_messages(user_id=uid, limit=n, since_utc=since_utc)
        if not msgs:
            return "(No conversation yet.)"
        lines: list[str] = []
        for m in msgs:
            role = "User" if m["role"] == "user" else "Assistant"
            lines.append(f"{role}: {m['content']}")
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
        input_reader: Callable[[], Awaitable[InboundMessage]] | None = None,
        response_sink: Callable[[int, str], Awaitable[None]] | None = None,
        main_user_id: int = 0,
    ):
        self.states: dict[str, StateConfig] = {}
        self.context = SessionContext(main_user_id=int(main_user_id or 0))
        self.current_state_name = initial_state
        # Per-state conversation history for pydantic-ai message continuity
        self._histories: dict[tuple[str, int], list[ModelMessage]] = {}
        self._pending_auto_run = False
        self._input_reader = input_reader or self._read_stdin
        self._response_sink = response_sink
        self._scheduled_timer_task: asyncio.Task | None = None
        self._scheduled_timer_target_utc: int | None = None
        self._stop_requested = False
        self._stop_reason = ""
        self._last_llm_call_at = 0.0

    def add_state(self, config: StateConfig):
        self.states[config.name] = config
        # histories are keyed by (state_name, user_id)

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
            # Clear per-user histories for this state.
            for key in [k for k in self._histories.keys() if k[0] == target]:
                del self._histories[key]
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
        try:
            while True:
                if self._stop_requested:
                    detail = f": {self._stop_reason}" if self._stop_reason else ""
                    print(f"[machine] Stopping{detail}")
                    break

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

                # Set up scheduled self-message timer (persisted in SQLite).
                stuck_seconds = int(os.environ.get("SCHEDULE_STUCK_SECONDS", "300") or "300")
                self.context.memory_store.reclaim_stuck_delivering(stuck_seconds=max(30, min(stuck_seconds, 3600)))
                next_deliver_at = self.context.memory_store.next_pending_self_message_time_utc()
                scheduled_task = self._scheduled_timer_task
                if next_deliver_at is None:
                    if scheduled_task is not None and not scheduled_task.done():
                        scheduled_task.cancel()
                    scheduled_task = None
                    self._scheduled_timer_task = None
                    self._scheduled_timer_target_utc = None
                else:
                    # If we don't have a timer, or it targets the wrong time (e.g. a new earlier schedule),
                    # create a new one.
                    delay = max(0.0, float(next_deliver_at) - datetime.now(tz=timezone.utc).timestamp())
                    if (
                        scheduled_task is None
                        or scheduled_task.done()
                        or self._scheduled_timer_target_utc != int(next_deliver_at)
                    ):
                        if scheduled_task is not None and not scheduled_task.done():
                            scheduled_task.cancel()
                        scheduled_task = asyncio.create_task(asyncio.sleep(delay))
                        self._scheduled_timer_task = scheduled_task
                        self._scheduled_timer_target_utc = int(next_deliver_at)

                wait_for: list[asyncio.Task] = [input_task]
                if timer_task:
                    wait_for.append(timer_task)
                if scheduled_task:
                    wait_for.append(scheduled_task)

                done, _ = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)

                if input_task in done:
                    inbound = input_task.result()
                    if not inbound or not inbound.text:
                        # EOF (e.g. Ctrl+D / Ctrl+Z)
                        print("\n[machine] Input closed.")
                        break
                    if timer_task:
                        timer_task.cancel()
                    input_task = asyncio.create_task(self._input_reader())

                    self.context.set_active_user(inbound.user_id)
                    user_input = inbound.text.strip()
                    if user_input:
                        self.context.memory_store.append_chat_message(
                            user_id=int(inbound.user_id),
                            role="user",
                            content=user_input,
                            state=state.name,
                            chat_id=inbound.chat_id,
                        )
                        self.context.log_event("user", user_input, state.name)
                        await self._run_agent(state, inbound.user_id, user_input)

                elif timer_task and timer_task in done:
                    # Idle timeout fired
                    self.transition_to(state.idle_target, f"idle for {state.idle_timeout}s")
                    continue

                elif scheduled_task and scheduled_task in done:
                    # Deliver any due scheduled self-messages by running them through standard state.
                    self._scheduled_timer_task = None
                    await self._deliver_scheduled_self_messages()
                    continue
        finally:
            if input_task and not input_task.done():
                input_task.cancel()

    async def _run_agent(self, state: StateConfig, user_id: int, user_input: str):
        """Run the agent for the given state with user input."""
        uid = int(user_id or 0)
        self.context.set_active_user(uid)
        prompt = (
            f"{self.context.memory_blocks_prompt()}\n\n"
            f"USER ID: {uid}\n"
            f"USER INPUT:\n{user_input}"
        )
        response = await self._run_agent_with_rate_limit_retry(
            state,
            prompt,
            message_history=self._histories.get((state.name, uid), []),
            max_completion_tokens=_env_int("MAX_COMPLETION_TOKENS", 4096),
        )
        if response is None:
            return

        self.context.log_event("assistant", response, state.name)
        print(f"[{state.name}]: {response}")
        if self._response_sink is not None:
            try:
                await self._response_sink(uid, response)
            except Exception as sink_err:
                print(f"[machine] Response sink error: {sink_err}")

        self.context.memory_store.append_chat_message(
            user_id=uid,
            role="assistant",
            content=response,
            state=state.name,
            chat_id=None,
        )
        self._check_transition()

    async def _deliver_scheduled_self_messages(self):
        # Fetch due messages from SQLite and run them through the standard agent so they have access
        # to the broadest toolset (and avoid journaling/identity update constraints).
        if "standard" not in self.states:
            return

        claimed = self.context.memory_store.claim_due_self_messages(limit=10)
        if not claimed:
            return

        standard_state = self.states["standard"]
        for item in claimed:
            msg_id = int(item["id"])
            created_at = int(item["created_at_utc"])
            deliver_at = int(item["deliver_at_utc"])
            message = str(item["message"])

            created_h = datetime.fromtimestamp(created_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            deliver_h = datetime.fromtimestamp(deliver_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            wrapped = (
                "SCHEDULED SELF MESSAGE\n"
                "(This message was previously scheduled and persisted; it may be delivered after a restart.)\n"
                f"ID: {msg_id}\n"
                f"Created: {created_h}\n"
                f"Scheduled for: {deliver_h}\n\n"
                f"{message}"
            )

            # Treat scheduled self-messages as belonging to main user for context purposes.
            uid = int(self.context.main_user_id or 0)
            self.context.set_active_user(uid)
            self.context.memory_store.append_chat_message(
                user_id=uid,
                role="user",
                content=wrapped,
                state="scheduled",
                chat_id=None,
            )
            self.context.log_event("user", wrapped, "scheduled")
            response = await self._run_agent_with_rate_limit_retry(
                standard_state,
                f"{self.context.memory_blocks_prompt()}\n\nUSER INPUT:\n{wrapped}",
                message_history=self._histories.get(("standard", uid), []),
                max_completion_tokens=_env_int("MAX_COMPLETION_TOKENS", 4096),
            )
            if response is None:
                self.context.memory_store.mark_self_message_failed(
                    message_id=msg_id, error="Agent run failed while delivering scheduled message.", retry_delay_seconds=60
                )
                continue

            self.context.log_event("assistant", response, "standard")
            print(f"[standard]: {response}")
            if self._response_sink is not None:
                try:
                    await self._response_sink(uid, response)
                except Exception as sink_err:
                    print(f"[machine] Response sink error: {sink_err}")

            self.context.memory_store.append_chat_message(
                user_id=uid,
                role="assistant",
                content=response,
                state="standard",
                chat_id=None,
            )
            self.context.memory_store.mark_self_message_delivered(message_id=msg_id)
            self._check_transition()

    async def _run_agent_autonomous(self):
        """Run the current state's agent without user input (e.g. journaling on idle)."""
        state = self.current

        if state.name == "human_update":
            await self._run_human_updates_for_recent_users()
            # Check for tool-requested transition first
            if not self._check_transition():
                if state.auto_return:
                    self.transition_to(state.auto_return, "task complete")
            return

        # Journaling + identity updates should be grounded in the main user's chat.
        uid = int(self.context.main_user_id or 0)
        self.context.set_active_user(uid)

        recent = self.context.recent_conversation(user_id=uid, n=_env_int("AUTONOMOUS_RECENT_MESSAGES", 20))
        prompt = (
            f"{self.context.memory_blocks_prompt()}\n\n"
            f"Recent conversation (user_id={uid}):\n{recent}\n\n"
            "Please proceed with your task."
        )

        response = await self._run_agent_with_rate_limit_retry(
            state,
            prompt,
            # Autonomous states shouldn't accumulate history indefinitely.
            message_history=[],
            max_completion_tokens=_env_int("AUTONOMOUS_MAX_COMPLETION_TOKENS", 2048),
        )
        if response is not None:
            self.context.log_event("assistant", response, state.name)
            print(f"[{state.name}]: {response}")
            if self._response_sink is not None:
                try:
                    await self._response_sink(uid, response)
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
        max_completion_tokens: int,
    ) -> str | None:
        """Run agent once, and retry once after cooldown for 429/401.

        Notes:
        - Adds a configurable per-call delay between LLM calls (env: LLM_CALL_DELAY_SECONDS).
        - Treats HTTP 401 as rate-limited (cooldown + retry) for now.
        - Cancels the whole state machine only if we receive the same code twice in a row
          (two 429s or two 401s).
        """
        max_history = _env_int("MAX_MESSAGE_HISTORY", 50)
        trimmed_history = list(message_history or [])[-max(0, int(max_history)) :]

        max_context_tokens = _env_int("MODEL_CONTEXT_LENGTH", 256000)
        margin_tokens = _env_int("PROMPT_TOKEN_MARGIN", 2048)
        fitted_prompt, fitted_history, fitted_max_completion = _fit_prompt_and_history_to_context(
            prompt=prompt,
            message_history=trimmed_history,
            max_context_tokens=max_context_tokens,
            max_completion_tokens=max_completion_tokens,
            margin_tokens=margin_tokens,
        )
        consecutive_429 = 0
        consecutive_401 = 0
        for attempt in (1, 2):
            try:
                await self._llm_call_delay()
                result = await state.agent.run(
                    fitted_prompt,
                    deps=self.context,
                    message_history=fitted_history,
                    model_settings={"max_tokens": fitted_max_completion},
                )
                all_msgs = result.all_messages()
                uid = int(self.context.active_user_id or 0)
                self._histories[(state.name, uid)] = list(all_msgs)[-max(0, int(max_history)) :]
                return result.output
            except Exception as err:
                should_retry, bucket = _llm_should_cooldown_and_retry(err)
                if not should_retry:
                    print(f"[{state.name}] Error: {err}")
                    print_exception_debug(state.name, err)
                    return None

                if bucket == 401:
                    consecutive_401 += 1
                    consecutive_429 = 0
                else:
                    consecutive_429 += 1
                    consecutive_401 = 0

                if consecutive_429 >= 2:
                    self._request_stop(f"received HTTP 429 twice in a row from LLM provider ({_rate_limit_detail(err)})")
                    return None
                if consecutive_401 >= 2:
                    self._request_stop(f"received HTTP 401 twice in a row from LLM provider ({_rate_limit_detail(err)})")
                    return None

                if attempt >= 2:
                    print(f"[{state.name}] Error after cooldown retry: {err}")
                    print_exception_debug(state.name, err)
                    return None

                detail = _rate_limit_detail(err)
                code_label = "401" if bucket == 401 else "429"
                print(f"[{state.name}] Provider returned HTTP {code_label}. Cooling down {RATE_LIMIT_WAIT_SECONDS}s then retrying. ({detail})")
                await asyncio.sleep(RATE_LIMIT_WAIT_SECONDS)

        return None

    async def _llm_call_delay(self) -> None:
        delay_s = max(0.0, float(_env_float("LLM_CALL_DELAY_SECONDS", 1.0)))
        if delay_s <= 0.0:
            return

        now = asyncio.get_running_loop().time()
        elapsed = now - float(self._last_llm_call_at or 0.0)
        if elapsed < delay_s:
            await asyncio.sleep(delay_s - elapsed)
            now = asyncio.get_running_loop().time()
        self._last_llm_call_at = now

    def _request_stop(self, reason: str) -> None:
        self._stop_requested = True
        self._stop_reason = reason.strip()

    def _check_transition(self) -> bool:
        """Check if any tool requested a transition. Returns True if one occurred."""
        transition = self.context.consume_transition()
        if transition:
            target, reason = transition
            return self.transition_to(target, reason)
        return False

    @staticmethod
    async def _read_stdin() -> InboundMessage:
        raw = await asyncio.to_thread(sys.stdin.readline)
        return InboundMessage(user_id=0, text=raw or "")

    async def _run_human_updates_for_recent_users(self) -> None:
        """Update per-user HUMAN blocks for every user who messaged in the last hour."""
        now = int(datetime.now(tz=timezone.utc).timestamp())
        window_seconds = _env_int("HUMAN_UPDATE_WINDOW_SECONDS", 3600)
        since_utc = max(0, now - max(60, int(window_seconds)))

        user_ids = self.context.memory_store.list_user_ids_with_recent_messages(since_utc=since_utc)
        if not user_ids:
            print("[human_update] No users with recent messages; skipping.")
            return

        state = self.current
        for uid in sorted({int(x) for x in user_ids}):
            self.context.set_active_user(uid)
            recent = self.context.recent_conversation(
                user_id=uid,
                n=_env_int("HUMAN_UPDATE_RECENT_MESSAGES", 40),
                since_seconds=window_seconds,
            )
            prompt = (
                f"{self.context.memory_blocks_prompt()}\n\n"
                f"Recent conversation (user_id={uid}, last {window_seconds}s):\n{recent}\n\n"
                "Please proceed with your task."
            )
            response = await self._run_agent_with_rate_limit_retry(
                state,
                prompt,
                message_history=[],
                max_completion_tokens=_env_int("AUTONOMOUS_MAX_COMPLETION_TOKENS", 2048),
            )
            if response is not None:
                self.context.log_event("assistant", response, state.name)
                print(f"[{state.name} user_id={uid}]: {response}")
