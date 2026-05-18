# Local Live Telegram Contour

Outcome: `live local Telegram contour certified`

## Live local Telegram scope

This step proves the first real local Telegram transport loop for AI Dev Team
on the user's Mac after `L0.1-L0.4`.

It does **not**:

- attach Hedgekeeper
- enable write-assisted main-project work
- expand the live roster to 20-30 agents
- start VPS rollout or production deploy
- claim a broad multi-agent discussion loop that was not actually observed

It does:

- start the real local Telegram runtime
- verify real Telegram reachability for the currently configured bot identities
- prove one bounded real inbound/outbound Telegram conversation
- confirm that Web Office and persisted state remain truthful around that live
  contour

## Exact startup preconditions

The local `.env` already satisfied the typed bot startup contract:

- `TELEGRAM_OWNER_CHAT_ID` was set
- `TELEGRAM_BOT_TOKEN` was set
- `TELEGRAM_AGENT_TOKENS` was set
- `OPENROUTER_API_KEY` was set
- startup config validation returned:
  `[bot] startup config validation passed`

The configured multi-bot runtime spec resolved truthfully to three live roles:

- `coordinator_agent`
- `reviewer_agent`
- `writer_agent`

For this certification run, the live contour also required writable isolated
local runtime paths:

- `STATE_DB_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/state/state.db`
- `OBS_LOG_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/logs/pipeline-log.jsonl`

The active local runtime seed remained the safe sandbox target from earlier
local steps:

- `REPO_PATH=/Users/efimenko_k/sandbox-project`

This step did **not** attach Hedgekeeper or any live Docker-mounted main
project path.

## Exact runtime path actually used

The live local Telegram contour was started on the Mac with the real runtime
entrypoint and the isolated local state/log overrides above:

```bash
STATE_DB_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/state/state.db \
OBS_LOG_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/logs/pipeline-log.jsonl \
.venv/bin/python scripts/run_telegram_bot.py --log-level INFO
```

The runtime path that actually executed was:

1. `scripts/run_telegram_bot.py`
2. `.env` load + typed startup validation
3. `build_multi_bot_runtime_spec_from_env(...)`
4. `BotIdentityLifecycleService` reachability probes (`getMe`) for each enabled
   bot identity
5. one PTB `Application` per enabled bot identity
6. real long-polling startup
7. real inbound owner Telegram message
8. `TelegramBridge` command routing
9. real outbound Telegram reply

Successful reachability + startup facts for the live local contour:

- `coordinator_agent` reachable as `@ai_dev_team_lead_bot` (`id=8747863811`)
- `reviewer_agent` reachable as `@ai_dev_team_reviewer_bot` (`id=8621415074`)
- `writer_agent` reachable as `@ai_dev_team_writer_bot` (`id=8505725687`)
- all three PTB applications reached `Application started`
- lifecycle summary marked all three roles as:
  - `token_valid=True`
  - `reachable=True`
  - `started=True`
  - `polling_started=True`

This is the first one that crossed the **real Telegram transport boundary** in the local track.

Earlier `L0.1-L0.4` proofs were still valuable, but they were transport-agnostic
or synthetic:

- local Web Office proof
- `/healthz` and `/readyz` proof
- backup proof
- synthetic owner-DM/control-path proof
- sandbox task execution proof

This step is the first one that proves **real Telegram delivery + real Telegram
reply**.

## First real conversation proof

Exact inbound user message:

- `/help`

Transport actually used:

- real Telegram DM to `@ai_dev_team_lead_bot`

Expected response scope before the run:

- prove owner-whitelist admission
- prove real inbound Telegram delivery
- prove command parsing through the live runtime
- prove outbound coordinator reply through the real Telegram path
- do this without pretending a broader multi-agent task loop has already been
  certified

Actual response observed in Telegram:

> `Координатор: 🛠 Доступные команды`

The real reply then listed the current operator command surface, including:

- `/project`
- `/projects`
- `/switch`
- `/team`
- `/budget`
- `/agents`
- `/tier`
- `/log`
- `/stop`
- `/retry`
- `/push`
- `/pr`
- `/help`

Visible roles / behaviour in this interaction:

- `coordinator_agent` was the only role surfaced in the first real reply
- `reviewer_agent` and `writer_agent` were already live and reachable at
  startup, but they were **not** invoked by `/help`
- this step does **not** claim a real multi-agent discussion loop yet

## Persisted / operator-visible aftermath

The live contour remained healthy and operator-visible after the first real
Telegram reply.

Health surfaces read back against the same isolated live `STATE_DB_PATH`:

- `/healthz` returned:
  - `ok=true`
  - `project_registry_ready=true`
  - `schema_version=11`
  - `state_db_path=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/state/state.db`
  - `state_db_fallback_in_use=false`
- `/readyz` returned:
  - `ok=true`
  - `ready=true`
  - the same live `state_db_path`

Web Office remained truthful for the live contour:

- dashboard `/` rendered `sandbox-project`
- `/projects/sandbox_project` rendered the project overview
- `/projects/sandbox_project/team` kept the baseline team view truthful
- `/projects/sandbox_project/history` remained honest about the lack of
  persisted task activity

Direct SQLite readback from the live contour confirmed:

- `schema_version=11`
- `project_task_count=0`
- `thread_count=0`
- specialist roster still empty

That aftermath is expected and honest:

- `/help` is a live Telegram command proof
- it is **not** a project task
- it should not invent task history, project threads, or specialist changes

## Bounded observations

One local operator detail mattered during certification:

- the repo `.env` did not provide a canonical `STATE_DB_PATH`
- this live contour therefore used an explicit isolated `STATE_DB_PATH` and
  `OBS_LOG_PATH` override under `/private/tmp/...`

This did **not** require a product-semantics change.
It was a truthful local runtime choice for this certification run.

## What is still intentionally not done

This step still does **not** prove:

- a live free-text coding task over Telegram
- a real multi-agent discussion loop between coordinator, writer, and reviewer
- Hedgekeeper attach
- write-enabled assist-mode
- 20-30 live Telegram agents
- VPS rollout
- production deploy

## Handoff to the next local step

The next local step can now build on a real live Telegram contour instead of
synthetic-only proof.

The most natural bounded next proof would be one of:

- a live `/agents` or `/team` operator interaction
- a live `/tier set ...` operator interaction
- one very small live free-text task against the safe sandbox repo
- a first role-aware multi-agent conversation that is still local-only

Those are future steps. They are **not** claimed as completed here.
