def test_identity_prompt_includes_aspirations_and_llm_acknowledgement():
    from states.identity_update import create_identity_update_state

    state = create_identity_update_state()
    prompt = "\n".join(state.agent._system_prompts).lower()
    assert "aspirations" in prompt
    assert "llm" in prompt


def test_journaling_prompt_includes_curiosities_and_not_dream():
    from states.journaling import create_journaling_state

    state = create_journaling_state()
    prompt = "\n".join(state.agent._system_prompts).lower()
    assert "curiosities" in prompt
    assert "dream" not in prompt
