import subprocess
import sys


def _tool_names(state) -> set[str]:
    # pydantic-ai keeps registered @agent.tool functions in the function toolset.
    return set(state.agent._function_toolset.tools.keys())


def test_dangerzone_tool_not_present_by_default():
    from states.standard import create_standard_state

    state = create_standard_state()
    assert "run_bash_command" not in _tool_names(state)


def test_dangerzone_tool_present_when_enabled():
    from states.standard import create_standard_state

    state = create_standard_state(enable_dangerzone=True)
    assert "run_bash_command" in _tool_names(state)


def test_main_help_includes_dangerzone_flag():
    completed = subprocess.run(
        [sys.executable, "main.py", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    # argparse --help should exit 0
    assert completed.returncode == 0
    assert "--dangerzone" in (completed.stdout or "") + (completed.stderr or "")

