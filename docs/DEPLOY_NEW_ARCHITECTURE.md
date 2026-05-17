# Deploy Guide for the New Architecture

This document is the canonical operator-facing deployability runbook for the
current architecture after `C0.1` through `C0.4`.

It describes what is already real in the repository today:
- canonical env layout
- startup validation
- local health semantics
- local verified `state.db` backups
- Telegram runtime entrypoint
- Web Office runtime entrypoint

It does not describe `C1.*` rollout as if it already exists. There is no
systemd unit, nginx config, HTTPS setup, cron backup automation, remote backup
upload, or VPS bootstrap in this step.

## Scope

Use this guide when you need to:
- prepare a truthful `.env` for the current runtime
- run the Telegram surface locally
- run the Web Office surface locally
- understand startup validation and health semantics
- create and verify a local backup of `state.db`
- check whether the repo is ready to begin a hosting rollout

Do not use this guide as a completed VPS or production-hosting manual. That
belongs to the later `C1.*` rollout work.

## Current Architecture

The current runtime has two operator-facing surfaces that share one persisted
SQLite state file:

1. Telegram runtime
   - entrypoint: `scripts/run_telegram_bot.py`
   - purpose: coordinator-led chat control plane, multi-project execution, and
     multi-bot startup when `TELEGRAM_AGENT_TOKENS` is configured
2. Web Office runtime
   - app module: `web.main:app`
   - purpose: project-aware HTML and API surfaces over the same persisted state
3. Shared persistence
   - canonical file: `state.db`
   - resolved from `STATE_DB_PATH`, then `BOT_STATE_DIR/state.db`, then the
     default home fallback path when that contract allows it

Shared deployability foundations already implemented:
- env contract in `.env.example` and `core/env_layout.py`
- startup validation in `core/startup_config_validation.py`
- local health model in `core/healthcheck_model.py`
- verified local backup primitive in `core/state_db_backup.py`

## Canonical Environment Layout

The current env contract is defined by `.env.example` and
`core/env_layout.py`.

### Canonical shared runtime env

- `STATE_DB_PATH`
  - primary persisted SQLite path for both Telegram and Web Office
- `OBS_LOG_PATH`
  - optional JSONL observability sink
- `LOG_LEVEL`
  - optional logging override for `scripts/run_telegram_bot.py`

### Canonical bot runtime env

- `TELEGRAM_OWNER_CHAT_ID`
  - required startup ownership/whitelist contract
- `TELEGRAM_BOT_TOKEN`
  - canonical coordinator token and single-bot compatibility path
- `TELEGRAM_AGENT_TOKENS`
  - canonical multi-bot role-to-env-key mapping input
- `OPENROUTER_API_KEY`
  - optional LLM routing key
- `OPENAI_API_KEY`
  - optional Whisper-only voice transcription key
- `BOT_COST_THRESHOLD_USD`
  - optional confirmation threshold

### Canonical web runtime env

- `STATE_DB_PATH`
  - Web Office reads the same persisted SQLite state contract as the bot

### Legacy compatibility / bootstrap env

- `BOT_STATE_DIR`
  - compatibility fallback only; if `STATE_DB_PATH` is unset, the runtime falls
    back to `BOT_STATE_DIR/state.db`
- `REPO_PATH`
  - legacy single-project bootstrap seed only; not the primary truth once the
    persisted registry already contains runtime-bound projects
- `WORKTREE_ROOT`
  - optional legacy bootstrap override for worktree placement

## Startup Validation Semantics

Startup validation is implemented in `core/startup_config_validation.py`.

### Bot startup validation

The Telegram entrypoint performs a local startup-config validation before deep
runtime startup.

Fatal errors today include:
- missing `TELEGRAM_OWNER_CHAT_ID`
- invalid `TELEGRAM_OWNER_CHAT_ID`
- no usable bot identity startup path
- invalid `TELEGRAM_AGENT_TOKENS`
- missing referenced token env key
- invalid `BOT_COST_THRESHOLD_USD`

Warnings today can include:
- legacy `REPO_PATH` issues
- legacy `WORKTREE_ROOT` issues
- unknown `LOG_LEVEL`

Intentionally non-fatal today:
- missing `OPENROUTER_API_KEY`
- missing `OPENAI_API_KEY`
- missing `REPO_PATH`
- missing `WORKTREE_ROOT`

Important distinction:
- startup validation is local config validation
- it is not the same thing as runtime health monitoring

### Web startup validation

The Web Office runtime uses a narrow validation seam through
`validate_web_startup_config(...)`.

That seam keeps accepted fallback semantics:
- explicit `STATE_DB_PATH`
- `BOT_STATE_DIR -> state.db` compatibility fallback
- default home fallback path where current contract allows it

## Health Semantics

The canonical local health model lives in `core/healthcheck_model.py`.

### Health states

- `ok`
  - local runtime pieces are available and there are no current degraded
    signals
- `degraded`
  - runtime is still usable, but there are non-fatal local signals such as
    startup warnings or fallback paths in use
- `failed`
  - local runtime truth is broken, such as missing critical runtime objects or
    startup validation errors

### `/healthz`

`/healthz` is the local liveness surface for Web Office. It is backed by the
canonical health model and returns the accepted payload shape:
- `ok`
- `app`
- `schema_version`
- `project_registry_ready`
- `state_db_path`
- `state_db_fallback_in_use`

### `/readyz`

`/readyz` is the local readiness surface for Web Office. It uses the same
canonical health model and adds:
- `ready`

### What the health model does not claim

The current health model is local-only. It does not yet include:
- Telegram reachability probing as a health surface
- OpenRouter probing
- uptime monitor behavior
- alert delivery
- remote diagnostics

Runtime startup can still perform other checks in its own flow, but `/healthz`
and `/readyz` are not a full ops monitor.

## Backup Semantics

The canonical local backup primitive lives in:
- `core/state_db_backup.py`
- `scripts/backup_state_db.py`

### What it does

- creates a SQLite-consistent snapshot of `state.db`
- uses the SQLite backup API instead of a blind file copy
- writes a backup artifact plus a compact manifest sidecar
- verifies the resulting artifact locally before reporting success

### What verification checks

- backup file exists
- backup file opens as SQLite
- `schema_version` can be read
- artifact metadata is truthful
- manifest matches the artifact metadata

### What it does not do yet

- no cron scheduling
- no remote backup upload
- no retention rotation automation
- no compression or encryption layer
- no restore orchestration for production failover

## Local Operator Flows

### 1. Prepare `.env`

Copy the template:

```bash
cp .env.example .env
```

Minimum values for a truthful local operator setup:
- `STATE_DB_PATH`
- `TELEGRAM_OWNER_CHAT_ID`
- `TELEGRAM_BOT_TOKEN`

Common optional values:
- `TELEGRAM_AGENT_TOKENS`
- `OPENROUTER_API_KEY`
- `OPENAI_API_KEY`
- `BOT_COST_THRESHOLD_USD`
- `OBS_LOG_PATH`
- `LOG_LEVEL`

Legacy-only values:
- `BOT_STATE_DIR`
- `REPO_PATH`
- `WORKTREE_ROOT`

### 2. Run the Telegram runtime locally

Canonical command:

```bash
.venv/bin/python scripts/run_telegram_bot.py
```

What to expect:
- `.env` is loaded first
- logging is configured
- startup config validation runs before deep runtime startup
- fatal config blockers exit early with a deterministic error
- warnings are logged but do not become fake hard failures

Possible early failure categories:
- invalid/missing owner chat id
- invalid bot identity startup contract
- invalid backup or state-path env wiring

### 3. Run the Web Office locally

Canonical command:

```bash
.venv/bin/python -m uvicorn web.main:app --host 127.0.0.1 --port 8000
```

Then open:
- [Dashboard](http://127.0.0.1:8000/)
- [healthz](http://127.0.0.1:8000/healthz)
- [readyz](http://127.0.0.1:8000/readyz)

What to expect:
- Web Office reads the same persisted `STATE_DB_PATH`
- the root page `/` is the Dashboard view
- `/healthz` and `/readyz` report local runtime truth
- import-safe fallback remains supported where current semantics allow it

### 4. Create a local verified backup

Canonical command:

```bash
.venv/bin/python scripts/backup_state_db.py
```

Optional explicit target dir:

```bash
.venv/bin/python scripts/backup_state_db.py --backup-dir /Users/you/state-db-backups
```

Success path:
- prints compact JSON to stdout
- includes `backup_path`, `manifest_path`, `schema_version`, `verified`, and
  `verification_detail`

Failure path:
- prints a deterministic `backup_state_db_failed:<code>: ...` message to stderr
- exits non-zero

## Pre-Hosting Readiness Checklist

Before starting any `C1.*` hosting rollout, verify all of the following:

- `.env` exists and matches the current canonical contract
- `STATE_DB_PATH` resolves truthfully
- `.venv/bin/python scripts/run_telegram_bot.py` reaches a clean local startup
  path for the intended env
- `.venv/bin/python -m uvicorn web.main:app --host 127.0.0.1 --port 8000`
  starts locally
- `/healthz` and `/readyz` return truthful local results
- `.venv/bin/python scripts/backup_state_db.py` succeeds locally
- operator understands which current behaviors are local/manual only

## What Is Already Real

- canonical env contract
- standardized env loading
- startup validation with fatal vs warning distinction
- local health model with `ok / degraded / failed`
- Web Office health wiring through `/healthz` and `/readyz`
- local verified SQLite-consistent backup primitive
- Telegram runtime entrypoint
- Web Office runtime entrypoint

## What Is Not Implemented Yet

- no systemd unit in the current step
- no nginx config in the current step
- no domain / HTTPS rollout in the current step
- no cron backup automation in the current step
- no remote backup push in the current step
- no hosting alerting / ops automation in the current step
- no VPS rollout in the current step

## Related Files

- `.env.example`
- `README.md`
- `core/env_layout.py`
- `core/startup_config_validation.py`
- `core/healthcheck_model.py`
- `core/state_db_backup.py`
- `scripts/run_telegram_bot.py`
- `scripts/backup_state_db.py`
- `web/main.py`
- `docs/ROADMAP_TO_PRODUCTION.md`
