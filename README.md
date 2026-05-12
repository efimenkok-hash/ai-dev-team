# AI Dev Team

AI Dev Team is a Telegram-driven multi-agent engineering bot for autonomous work on real git repositories. It runs a fixed FSM pipeline over specialised agents, writes changes into isolated git worktrees, validates them with `ruff` and `pytest`, and can commit, push, and open draft PRs.

Current production scope: coordinator-led Telegram control plane, one active worker, real OpenRouter pipeline, SQLite-backed state, project-aware execution resolved from bound project chats or the single-project owner-DM/bootstrap fallback, and an entrypoint that can launch multiple Telegram bot identities from `TELEGRAM_AGENT_TOKENS` with role-aware outbound sending plus coordinator fallback when a specific identity is unavailable. Bound project chats now receive agent lifecycle progress from the matching bot identity when that sender exists, owner private DMs can stay on the exact bot identity thread the owner used, and startup now validates each configured bot identity with a reachability probe before it is considered started.

## Status

| Item | Value |
|---|---|
| Tests | 2006 passed, 5 skipped |
| Ruff | clean |
| Python | >= 3.10 |
| Real pipeline | OpenRouter + git worktree + commit + project-aware `/push` + project-aware `/pr` |
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

If `OPENROUTER_API_KEY` is missing, or if the bot cannot resolve any routable project runtime, it still starts, but only in the simple acknowledgement mode rather than the full multi-agent pipeline.

## Environment

Required for the full pipeline:

- `OPENROUTER_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_OWNER_CHAT_ID`

Project runtime resolution for the full pipeline:

- Preferred: a Telegram message resolves to a persisted project snapshot in `StateDB`, then `ProjectRuntimeRouter` materializes the matching runtime binding into a sandbox.
- Bound project chats are first-class: if a chat is explicitly bound to a project, free-text tasks plus `/push` and `/pr` run in that project's repo/worktree context, even when the registry contains multiple projects.
- Owner DM fallback remains only for the single-project case: if the registry contains exactly one project, owner DM can still resolve that project without an explicit chat binding.
- Legacy fallback: `REPO_PATH` (and optional `WORKTREE_ROOT`) are now bootstrap-only. They seed or provide a single-project runtime when the registry is empty or unavailable.
- Current limit: when the registry contains multiple projects and the message itself is not bound to a project, the bot does not auto-select one.

Optional:

- `OPENAI_API_KEY` - enables Whisper voice transcription.
- `TELEGRAM_AGENT_TOKENS` - optional role-to-env-key mapping for multi-bot runtime startup. Format: `coordinator_agent=TELEGRAM_BOT_TOKEN,writer_agent=TELEGRAM_WRITER_BOT_TOKEN`. The values inside this string are env-var names, not raw Telegram tokens. When present, the entrypoint starts one PTB `Application` per configured identity, probes every configured bot with Telegram `get_me()` before startup, fails fast if any configured identity is invalid or unreachable, routes inbound messages through `MultiBotBridge`, and chooses outbound bot identity from the envelope `sender_role` or explicit `delivery_role` with coordinator fallback if a specific sender is unavailable. Secondary private owner DMs now stay on the identity thread the owner used; secondary group inbound still resolves to `secondary_bot_inbound_not_enabled`. Bound project chats still emit agent lifecycle progress through the matching bot identity.
- `REPO_PATH` - legacy bootstrap/fallback for single-project compatibility. It is no longer the sole source of runtime selection once projects with runtime bindings already exist in `StateDB`.
- `WORKTREE_ROOT` - optional legacy bootstrap override for the worktree root.
- `STATE_DB_PATH` - SQLite path for tier sessions, task history, budget state, and the project registry/runtime bindings.
- `BOT_STATE_DIR` - legacy compatibility directory. If `STATE_DB_PATH` is unset, the bot uses `BOT_STATE_DIR/state.db`.
- `BOT_COST_THRESHOLD_USD` - confirmation threshold for expensive tasks.
- `OBS_LOG_PATH` - JSONL log sink for observability and cost snapshots.
- `AI_DEV_TEAM_REAL_LLM=1` - enables the opt-in real integration test.

## Commands

Available Telegram commands:

- `/project` - show the current project context for this chat/owner-DM fallback; does not change routing
- `/projects` - list registered projects, runtime binding state, and explicit chat bindings
- `/projects here` - show explicit binding status for the current chat
- `/projects bind <project_id_or_slug>` - bind the current Telegram group/supergroup chat to an existing runtime-bound project (owner only)
- `/projects migrate here` - migrate the current Telegram group/supergroup chat from legacy single-project bootstrap/fallback into an explicit project chat when exactly one migratable project exists (owner only)
- `/projects unbind` - remove the explicit project binding from the current chat (owner only)
- `/switch` - read-only project-context helper; it does not select or switch runtime-projects
- `/budget`
- `/agents`
- `/tier`
- `/log`
- `/stop`
- `/retry`
- `/push`
- `/pr`
- `/help`

Project context requirements today:

- `/project` shows the current resolved project context, including whether it came from an explicit project chat or owner-DM fallback
- free-text tasks require a resolved project runtime
- `/push` requires a resolved project runtime
- `/pr` requires a resolved project runtime
- explicit project chat bindings are managed through `/projects bind` and `/projects unbind`
- `/projects migrate here` is the migration path from legacy single-project bootstrap to an explicit project chat; it works only in group/supergroup chats and only when exactly one runtime-bound project exists without an explicit chat binding
- if multiple projects already exist, migration here is not used; bind the target chat explicitly with `/projects bind <project_id_or_slug>`
- explicit project chats are group/supergroup chats only (`chat_id < 0`)
- `/project` is read-only and never changes project resolution or runtime routing
- `/switch` is navigation/status only and never changes project resolution or runtime routing
- owner DM without explicit chat binding works only when the registry has exactly one project
- unbound chats with multiple projects do not get an implicit runtime

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
