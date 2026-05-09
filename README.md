# AI Dev Team

AI Dev Team is a Telegram-driven multi-agent engineering bot for autonomous work on real git repositories. It runs a fixed FSM pipeline over specialised agents, writes changes into isolated git worktrees, validates them with `ruff` and `pytest`, and can commit, push, and open draft PRs.

Current production scope: one Telegram bot, one active worker, real OpenRouter pipeline, SQLite-backed state, and single-project execution resolved from the project registry or a legacy `REPO_PATH` bootstrap.

## Status

| Item | Value |
|---|---|
| Tests | 2006 passed, 5 skipped |
| Ruff | clean |
| Python | >= 3.10 |
| Real pipeline | OpenRouter + git worktree + commit + `/push` + `/pr` |
| Persistence | SQLite (`STATE_DB_PATH`) with legacy fallback |

## What Works Today

- Telegram bot with owner whitelist, voice input via Whisper, and image input via vision client.
- Tier-aware orchestration: `/tier set ECONOMY|STANDARD|PREMIUM` changes model fallback chains.
- FSM pipeline: `PLANNING -> PM -> ARCHITECT -> WRITER -> REVIEW -> TEST -> QA -> SUCCESS` with fix loops and retry limits.
- Real sandbox execution in git worktrees under `feature/<task_id>`.
- Writer output materialisation to disk with path traversal protection.
- Auto-fix before strict validation: `ruff format` + `ruff check --fix`, then strict `ruff` / `pytest`.
- Additive-change preservation guard for tasks that should extend existing code instead of replacing it.
- Task history, tier sessions, and budget state persisted in SQLite.
- `/push` for real GitHub push and `/pr` for `gh pr create --draft`.
- `/stop` for real task cancellation without fake completion.

## Repository Layout

Key runtime modules:

```text
core/agent_personas.py      frozen personas and voice traits
core/bot_commands.py        slash-command parsing and registry
core/bot_runner.py          env-driven composition root for the bot
core/dispatcher_agents.py   production prompts for planning/pm/architect/...
core/llm_dispatcher.py      OpenRouter client + model fallback chains
core/model_tier.py          ECONOMY / STANDARD / PREMIUM registry
core/orchestrator.py        FSM driver and repair loops
core/progress_emitter.py    typed progress events for streaming
core/real_task_handler.py   bridge <-> pipeline glue
core/sandbox_workspace.py   git worktree lifecycle, commit, push, PR
core/sandbox_runtime_hook.py runtime validation + preservation guard
core/sandbox_autofix.py     ruff format / fix pre-pass
core/runtime_validator.py   strict ruff / pytest execution
core/state_db.py            SQLite persistence layer
core/task_history.py        task history backed by SQLite or memory
core/telegram_bridge.py     transport-agnostic Telegram logic
core/tier_session.py        per-chat tier persistence and migration
core/observability.py       JSONL observability and cost snapshots
core/writer_to_worktree.py  JSON -> filesystem materialiser
scripts/run_telegram_bot.py PTB long-polling entry point
```

Supporting analysis modules such as `call_graph.py`, `dependency_graph.py`, `impact_analysis.py`, `repo_reader.py`, and `vector_store.py` are still present in the repo, but the production Telegram path is centered around the modules above.

## Quick Start

```bash
git clone <your-fork-url> ai-dev-team
cd ai-dev-team

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
```

Minimum `.env` for legacy-compatible single-project bootstrap:

```dotenv
OPENROUTER_API_KEY=sk-or-v1-...
TELEGRAM_BOT_TOKEN=1234567890:...
TELEGRAM_OWNER_CHAT_ID=123456789
REPO_PATH=/Users/you/sandbox-project
```

Recommended local checks:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest
```

Run the Telegram bot:

```bash
.venv/bin/python scripts/run_telegram_bot.py
```

If `OPENROUTER_API_KEY` is missing, or if the bot cannot resolve one active project with a valid runtime binding, it still starts, but only in the simple acknowledgement mode rather than the full multi-agent pipeline.

## Environment

Required for the full pipeline:

- `OPENROUTER_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_OWNER_CHAT_ID`

Active project resolution for the full pipeline:

- Preferred: one persisted project in `StateDB` with a valid runtime binding.
- Legacy fallback: `REPO_PATH` (and optional `WORKTREE_ROOT`) bootstrap a single project when the registry is empty.
- Current limit: if the registry contains multiple projects, the bot does not auto-select one yet and stays in simple mode.

Optional:

- `OPENAI_API_KEY` - enables Whisper voice transcription.
- `REPO_PATH` - legacy bootstrap/fallback for single-project compatibility. Once `StateDB` already has one project with a runtime binding, `REPO_PATH` is no longer required for startup.
- `WORKTREE_ROOT` - optional legacy bootstrap override for the worktree root.
- `STATE_DB_PATH` - SQLite path for tier sessions, task history, budget state, and the project registry/runtime bindings.
- `BOT_STATE_DIR` - legacy compatibility directory. If `STATE_DB_PATH` is unset, the bot uses `BOT_STATE_DIR/state.db`.
- `BOT_COST_THRESHOLD_USD` - confirmation threshold for expensive tasks.
- `OBS_LOG_PATH` - JSONL log sink for observability and cost snapshots.
- `AI_DEV_TEAM_REAL_LLM=1` - enables the opt-in real integration test.

## Commands

Available Telegram commands:

- `/projects`
- `/switch`
- `/budget`
- `/agents`
- `/tier`
- `/log`
- `/stop`
- `/retry`
- `/push`
- `/pr`
- `/help`

## Quality Gates

The runtime path validates generated code through:

1. writer output materialisation into a sandbox worktree
2. auto-fix (`ruff format` + `ruff check --fix`)
3. strict validation (`ruff check .` + `pytest`)
4. fixer loop when reviewer/tester/qa detect a recoverable issue

Local validation mirrors that flow with:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest
```

## Real E2E Test

The opt-in integration test exercises the real OpenRouter pipeline against a temporary git repository:

```bash
AI_DEV_TEAM_REAL_LLM=1 OPENROUTER_API_KEY=sk-or-v1-... \
    .venv/bin/python -m pytest tests/integration/test_real_pipeline_e2e.py -v -s
```

It does not use your real `REPO_PATH`; the test creates its own isolated temp repo.

## Roadmap

The active production roadmap lives in [docs/ROADMAP_TO_PRODUCTION.md](docs/ROADMAP_TO_PRODUCTION.md). Current priorities after pipeline validation are VPS hosting, web office, multi-bot architecture, UX polish, and dynamic team expansion.

## License

MIT - see [LICENSE](LICENSE).
