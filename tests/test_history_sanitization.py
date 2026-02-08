from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

from machine import _sanitize_message_history


def test_sanitize_removes_system_prompt_parts_and_instructions() -> None:
    req = ModelRequest(
        parts=[
            SystemPromptPart(content="SYSTEM"),
            UserPromptPart(content="USER INPUT:\nHello"),
        ],
        instructions="INSTRUCTIONS",
    )
    out = _sanitize_message_history([req])
    assert len(out) == 1
    assert isinstance(out[0], ModelRequest)
    assert out[0].instructions is None
    assert all(not isinstance(p, SystemPromptPart) for p in out[0].parts)


def test_sanitize_strips_legacy_inline_memory_blocks_from_user_prompt() -> None:
    legacy = (
        "MEMORY BLOCKS\n"
        "IDENTITY:\n"
        "x\n\n"
        "HUMAN (user_id=0):\n"
        "y\n\n"
        "RECENT JOURNAL ENTRIES:\n"
        "(No journal entries yet.)\n\n"
        "USER INPUT:\nHello"
    )
    req = ModelRequest(parts=[UserPromptPart(content=legacy)])
    out = _sanitize_message_history([req])
    assert len(out) == 1
    user_part = next(p for p in out[0].parts if isinstance(p, UserPromptPart))
    assert isinstance(user_part.content, str)
    assert user_part.content.startswith("USER INPUT:\nHello")


def test_sanitize_drops_system_only_requests() -> None:
    req = ModelRequest(parts=[SystemPromptPart(content="SYSTEM")])
    out = _sanitize_message_history([req])
    assert out == []

