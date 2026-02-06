import os

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from debug_auth import get_openrouter_api_key
from machine import SessionContext, StateConfig


def create_identity_update_state() -> StateConfig:
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
            "You maintain the IDENTITY memory block for the assistant. "
            "Review the memory blocks and recent conversation provided in the prompt. "
            "Choose exactly one action:\n"
            "1) keep_identity_as_is(reason) when current identity is still accurate\n"
            "2) rewrite_identity(new_identity, reason) when it should be updated\n"
            "If rewriting, output a concise, structured block with Name, Style, and Personality. "
            "Always call one tool exactly once before your final response."
        ),
        deps_type=SessionContext,
    )

    @agent.tool
    def keep_identity_as_is(ctx: RunContext[SessionContext], reason: str = "") -> str:
        """Keep the IDENTITY block unchanged."""
        detail = f" Reason: {reason}" if reason else ""
        return f"IDENTITY kept as-is.{detail}"

    @agent.tool
    def rewrite_identity(
        ctx: RunContext[SessionContext], new_identity: str, reason: str = ""
    ) -> str:
        """Rewrite the IDENTITY block."""
        ctx.deps.set_identity_block(new_identity)
        detail = f" Reason: {reason}" if reason else ""
        return f"IDENTITY rewritten.{detail}"

    return StateConfig(
        name="identity_update",
        agent=agent,
        auto_run_on_enter=True,
        auto_return="human_update",
    )
