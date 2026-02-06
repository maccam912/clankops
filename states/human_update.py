import os

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from debug_auth import get_openrouter_api_key
from machine import SessionContext, StateConfig
from tool_utils import safe_tool


def create_human_update_state() -> StateConfig:
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
            "You maintain the HUMAN memory block for the user. "
            "Review the memory blocks and recent conversation provided in the prompt. "
            "Choose exactly one action:\n"
            "1) keep_human_as_is(reason) when the block is still accurate\n"
            "2) rewrite_human(new_human, reason) when it should be updated\n"
            "Only include durable user info (likes, dislikes, biographical details, preferences). "
            "If rewriting, output a concise structured block with Name, Likes, Dislikes, Bio. "
            "Always call one tool exactly once before your final response."
        ),
        deps_type=SessionContext,
    )

    @agent.tool
    @safe_tool("keep_human_as_is")
    def keep_human_as_is(ctx: RunContext[SessionContext], reason: str = "") -> str:
        """Keep the HUMAN block unchanged."""
        detail = f" Reason: {reason}" if reason else ""
        return f"HUMAN kept as-is.{detail}"

    @agent.tool
    @safe_tool("rewrite_human")
    def rewrite_human(ctx: RunContext[SessionContext], new_human: str, reason: str = "") -> str:
        """Rewrite the HUMAN block."""
        ctx.deps.set_human_block(new_human)
        detail = f" Reason: {reason}" if reason else ""
        return f"HUMAN rewritten.{detail}"

    return StateConfig(
        name="human_update",
        agent=agent,
        auto_run_on_enter=True,
        auto_return="standard",
    )
