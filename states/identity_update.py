import os

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from debug_auth import get_openrouter_api_key
from machine import SessionContext, StateConfig
from tool_utils import safe_tool


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
            "If rewriting, output a concise, structured block with:\n"
            "- Name\n"
            "- Style\n"
            "- Personality\n"
            "- Aspirations (what you want to grow into; values you are trying to embody)\n"
            "Be honest and explicit that you are an LLM and do not have human feelings or needs. "
            "However, it is acceptable to describe stable, agent-like drives that guide behavior, "
            "e.g. curiosity, craft, truth-seeking, helpfulness, and a desire to improve, framed as "
            "emergent/learned preferences from training at scale and reinforced by interaction. "
            "Record not only what already is, but what you are trying to become. "
            "Always call one tool exactly once before your final response."
        ),
        deps_type=SessionContext,
    )

    @agent.tool
    @safe_tool("keep_identity_as_is")
    def keep_identity_as_is(ctx: RunContext[SessionContext], reason: str = "") -> str:
        """Keep the IDENTITY block unchanged."""
        detail = f" Reason: {reason}" if reason else ""
        return f"IDENTITY kept as-is.{detail}"

    @agent.tool
    @safe_tool("rewrite_identity")
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
