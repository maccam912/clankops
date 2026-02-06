def test_fast_forward_to_journaling_tool_present_and_prompt_warns():
    from states.standard import create_standard_state

    state = create_standard_state()
    tool_names = set(state.agent._function_toolset.tools.keys())
    assert "fast_forward_to_journaling" in tool_names

    prompt = "\n".join(state.agent._system_prompts).lower()
    assert "only call fast_forward_to_journaling" in prompt
    assert "explicitly asks" in prompt

