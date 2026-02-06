import os
from datetime import datetime

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from debug_auth import get_openrouter_api_key
from machine import SessionContext, StateConfig


def create_journaling_state() -> StateConfig:
    base_url = "https://openrouter.ai/api/v1"
    model_name = os.environ.get("MODEL_NAME", "google/gemini-2.0-flash-exp:free").strip()
    api_key = get_openrouter_api_key()

    provider = OpenAIProvider(
        base_url=base_url,
        api_key=api_key,
    )
    model = OpenAIModel(
        model_name,
        provider=provider,
    )

    agent: Agent[SessionContext, str] = Agent(
        model,
        system_prompt=(
            "You are a journaling agent. You activate when the user has been idle "
            "or requests reflection. Write a brief, thoughtful journal entry about the "
            "session so far, then call save_journal_entry to persist it. "
            "Be concise and genuine."
        ),
        deps_type=SessionContext,
    )

    @agent.tool
    def save_journal_entry(ctx: RunContext[SessionContext], entry: str) -> str:
        """Save a journal entry to the session log."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rendered = f"[{timestamp}] {entry.strip()}"
        ctx.deps.add_journal_entry(rendered)
        return f"Journal entry saved. ({len(ctx.deps.journal_entries)} total)"

    @agent.tool
    def get_journal_entries(ctx: RunContext[SessionContext]) -> str:
        """Retrieve all journal entries from this session."""
        if not ctx.deps.journal_entries:
            return "No journal entries yet."
        entries = []
        for i, entry in enumerate(ctx.deps.journal_entries, 1):
            entries.append(f"{i}. {entry}")
        return "\n".join(entries)

    return StateConfig(
        name="journaling",
        agent=agent,
        auto_run_on_enter=True,
        auto_return="identity_update",
    )
