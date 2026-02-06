# Repository Guidelines

## Project Structure & Module Organization
- Core entrypoint: `main.py` (loads environment, validates auth, starts the state machine).
- Orchestration logic: `machine.py` (`SessionContext`, `StateConfig`, `StateMachine`).
- State implementations: `states/standard.py`, `states/journaling.py`, `states/identity_update.py`, and `states/human_update.py`.
- Memory persistence: `memory_store.py` plus `memory/identity.txt`, `memory/human.txt`, `memory/journal/*.txt`, and `memory/memory.sqlite3`.
- Environment templates: `.env.example`; local overrides in `.env` (do not commit secrets).
- Dependency/runtime metadata: `pyproject.toml`, `uv.lock`, `.python-version` (Python 3.13).

## Build, Test, and Development Commands
- Install/sync dependencies:
  - `uv sync`
- Run the app locally:
  - `uv run python main.py`
- Quick syntax check:
  - `uv run python -m compileall .`

## Coding Style & Naming Conventions
- Follow PEP 8 with 4-space indentation and clear type hints (existing code uses typed dataclasses and annotated functions).
- Use `snake_case` for variables/functions/modules, `PascalCase` for classes, and short descriptive state/tool names.
- Keep state-specific tools close to their owning state factory (for example `create_journaling_state` and memory update states).
- Prefer small, focused functions and explicit transition reasons (`request_transition(target, reason)`).

## Testing Guidelines
- There is currently no committed `tests/` suite. Add new tests under `tests/` using `pytest` conventions (`test_*.py`).
- Focus coverage on:
  - State transitions and idle behavior in `machine.py`
  - Tool behavior in `states/*.py`
  - Memory persistence behavior in `memory_store.py`
- Run tests with:
  - `uv run pytest`
- For changes without tests yet, at minimum run `uv run python main.py` and verify startup + transitions manually.

## Commit & Pull Request Guidelines
- No project commit history exists yet; use Conventional Commit style going forward:
  - `feat: add journaling transition guard`
  - `fix: trim bearer prefix before auth header`
- PRs should include:
  - What changed and why
  - Any `.env`/configuration impact
  - Test evidence (command output or manual validation notes)
  - Linked issue/task when applicable

## Security & Configuration Tips
- Never commit real API keys; keep `OPENROUTER_API_KEY` in `.env`.
- Use `.env.example` as the source of truth for required variables (`OPENROUTER_API_KEY`, `MODEL_NAME`, `IDLE_TIMEOUT`, `MEMORY_DIR`, `SEARXNG_URL`).
