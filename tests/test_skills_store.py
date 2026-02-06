import os
from pathlib import Path

import pytest


def test_skills_store_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))

    import skills_store

    assert skills_store.list_skills(Path.cwd()) == []

    created = skills_store.upsert_skill(
        Path.cwd(),
        skill_id="Test Skill",
        description="One line description",
        body_markdown="Use this to do the thing.\n\nSteps:\n- A\n- B",
        display_name="Test Skill",
    )
    assert created["ok"] is True
    assert created["skill_id"] == "test-skill"

    metas = skills_store.list_skills(Path.cwd())
    assert len(metas) == 1
    assert metas[0].skill_id == "test-skill"
    assert "description" in metas[0].description.lower()

    md = skills_store.read_skill_markdown(Path.cwd(), "test-skill", max_chars=2000)
    assert md["skill_id"] == "test-skill"
    assert md["truncated"] is False
    assert "One line description" in str(md["content"])
    assert "# Test Skill" in str(md["content"])

    with pytest.raises(ValueError):
        skills_store.delete_skill(Path.cwd(), "test-skill", confirm=False)

    deleted = skills_store.delete_skill(Path.cwd(), "test-skill", confirm=True)
    assert deleted["ok"] is True
    assert deleted["deleted"] is True

    assert skills_store.list_skills(Path.cwd()) == []


def test_standard_state_exposes_skills_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))

    from states.standard import create_standard_state

    state = create_standard_state()
    tool_names = set(state.agent._function_toolset.tools.keys())
    assert "list_skills" in tool_names
    assert "read_skill" in tool_names
    assert "upsert_skill" in tool_names
    assert "delete_skill" in tool_names

    prompt = "\n".join(state.agent._system_prompts).lower()
    assert "skills" in prompt

