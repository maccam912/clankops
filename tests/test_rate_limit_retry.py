import pytest


class _DummyResult:
    def __init__(self, output: str):
        self.output = output

    def all_messages(self):
        return []


class _RateLimitErr(Exception):
    def __init__(self):
        super().__init__("Rate limit exceeded: free-models-per-min.")
        self.status_code = 429
        self.model_name = "example/free"
        self.body = {"message": "Rate limit exceeded", "code": 429}


@pytest.mark.anyio
async def test_rate_limit_waits_and_retries_with_recovery_prompt(monkeypatch):
    from machine import RATE_LIMIT_WAIT_SECONDS, StateMachine, StateConfig

    calls: list[str] = []
    slept: list[float] = []
    sink_msgs: list[str] = []

    async def fake_sleep(seconds: float):
        slept.append(seconds)

    async def sink(msg: str):
        sink_msgs.append(msg)

    class DummyAgent:
        async def run(self, prompt, deps=None, message_history=None):  # noqa: ANN001
            calls.append(str(prompt))
            if len(calls) == 1:
                raise _RateLimitErr()
            return _DummyResult("ok after retry")

    monkeypatch.setattr("machine.asyncio.sleep", fake_sleep)

    m = StateMachine(initial_state="standard", response_sink=sink)
    state = StateConfig(name="standard", agent=DummyAgent())
    m.add_state(state)

    out = await m._run_agent_with_rate_limit_retry(state, "hello", message_history=[])
    assert out == "ok after retry"
    assert slept == [RATE_LIMIT_WAIT_SECONDS]
    assert len(calls) == 2
    assert "RATE LIMIT RECOVERY" in calls[1]
    assert any("Rate limit encountered" in s for s in sink_msgs)

