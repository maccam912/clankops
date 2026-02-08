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


class _UnauthorizedErr(Exception):
    def __init__(self):
        super().__init__("Unauthorized")
        self.status_code = 401
        self.model_name = "example/free"
        self.body = {"message": "Unauthorized", "code": 401}


@pytest.mark.anyio
async def test_rate_limit_waits_and_retries_reruns_same_prompt(monkeypatch):
    from machine import RATE_LIMIT_WAIT_SECONDS, StateMachine, StateConfig

    calls: list[str] = []
    slept: list[float] = []

    async def fake_sleep(seconds: float):
        slept.append(seconds)

    class DummyAgent:
        async def run(self, prompt, deps=None, message_history=None, model_settings=None):  # noqa: ANN001
            calls.append(str(prompt))
            if len(calls) == 1:
                raise _RateLimitErr()
            return _DummyResult("ok after retry")

    monkeypatch.setenv("LLM_CALL_DELAY_SECONDS", "0")
    monkeypatch.setattr("machine.asyncio.sleep", fake_sleep)

    m = StateMachine(initial_state="standard", response_sink=None)
    state = StateConfig(name="standard", agent=DummyAgent())
    m.add_state(state)

    out = await m._run_agent_with_rate_limit_retry(state, "hello", message_history=[], max_completion_tokens=1024)
    assert out == "ok after retry"
    assert slept == [RATE_LIMIT_WAIT_SECONDS]
    assert len(calls) == 2
    assert calls[1] == calls[0]


@pytest.mark.anyio
async def test_401_waits_and_retries_reruns_same_prompt(monkeypatch):
    from machine import RATE_LIMIT_WAIT_SECONDS, StateMachine, StateConfig

    calls: list[str] = []
    slept: list[float] = []

    async def fake_sleep(seconds: float):
        slept.append(seconds)

    class DummyAgent:
        async def run(self, prompt, deps=None, message_history=None, model_settings=None):  # noqa: ANN001
            calls.append(str(prompt))
            if len(calls) == 1:
                raise _UnauthorizedErr()
            return _DummyResult("ok after retry")

    monkeypatch.setenv("LLM_CALL_DELAY_SECONDS", "0")
    monkeypatch.setattr("machine.asyncio.sleep", fake_sleep)

    m = StateMachine(initial_state="standard", response_sink=None)
    state = StateConfig(name="standard", agent=DummyAgent())
    m.add_state(state)

    out = await m._run_agent_with_rate_limit_retry(state, "hello", message_history=[], max_completion_tokens=1024)
    assert out == "ok after retry"
    assert slept == [RATE_LIMIT_WAIT_SECONDS]
    assert len(calls) == 2
    assert calls[1] == calls[0]


@pytest.mark.anyio
async def test_two_429s_in_a_row_requests_stop(monkeypatch):
    from machine import RATE_LIMIT_WAIT_SECONDS, StateMachine, StateConfig

    slept: list[float] = []

    async def fake_sleep(seconds: float):
        slept.append(seconds)

    class DummyAgent:
        async def run(self, prompt, deps=None, message_history=None, model_settings=None):  # noqa: ANN001
            raise _RateLimitErr()

    monkeypatch.setenv("LLM_CALL_DELAY_SECONDS", "0")
    monkeypatch.setattr("machine.asyncio.sleep", fake_sleep)

    m = StateMachine(initial_state="standard", response_sink=None)
    state = StateConfig(name="standard", agent=DummyAgent())
    m.add_state(state)

    out = await m._run_agent_with_rate_limit_retry(state, "hello", message_history=[], max_completion_tokens=1024)
    assert out is None
    assert m._stop_requested is True
    assert slept == [RATE_LIMIT_WAIT_SECONDS]


@pytest.mark.anyio
async def test_llm_call_delay_sleeps_between_calls(monkeypatch):
    from machine import StateMachine, StateConfig

    slept: list[float] = []

    class _FakeLoop:
        def __init__(self):
            self.t = 100.0

        def time(self) -> float:
            return float(self.t)

    loop = _FakeLoop()

    async def fake_sleep(seconds: float):
        slept.append(seconds)
        loop.t += float(seconds)

    class DummyAgent:
        async def run(self, prompt, deps=None, message_history=None, model_settings=None):  # noqa: ANN001
            return _DummyResult("ok")

    monkeypatch.setenv("LLM_CALL_DELAY_SECONDS", "1.0")
    monkeypatch.setattr("machine.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("machine.asyncio.get_running_loop", lambda: loop)

    m = StateMachine(initial_state="standard", response_sink=None)
    state = StateConfig(name="standard", agent=DummyAgent())
    m.add_state(state)

    out1 = await m._run_agent_with_rate_limit_retry(state, "hello", message_history=[], max_completion_tokens=1024)
    out2 = await m._run_agent_with_rate_limit_retry(state, "hello", message_history=[], max_completion_tokens=1024)
    assert out1 == "ok"
    assert out2 == "ok"
    assert slept == [1.0]
