# Local Isolated Pilot Status

Checked on `2026-05-18`.

Canonical runbook:
- `docs/LOCAL_ISOLATED_PILOT_RUNBOOK.md`

## Outcome

`pilot blocked externally`

## Canonical Operator Layout

The canonical operator-facing local pilot layout is documented as:

- `~/ai-dev-team-local-pilot/state/state.db`
- `~/ai-dev-team-local-pilot/worktrees/`
- `~/ai-dev-team-local-pilot/logs/pipeline-log.jsonl`
- `~/ai-dev-team-local-pilot/targets/`
- `~/ai-dev-team-local-pilot/backups/`
- Web Office on `127.0.0.1:8001`

## Sandbox-Safe Verification Layout Used in This Step

Because this step was executed inside a sandboxed Codex workspace, the verified
local smoke layout used these exact isolated paths instead:

- state DB:
  `/private/tmp/ai-dev-team-local-pilot-smoke/state/state.db`
- worktree root:
  `/private/tmp/ai-dev-team-local-pilot-smoke/worktrees/`
- observability log path:
  `/private/tmp/ai-dev-team-local-pilot-smoke/logs/pipeline-log.jsonl`
- target repo root:
  `/private/tmp/ai-dev-team-local-pilot-smoke/targets/`
- disposable sandbox repo:
  `/private/tmp/ai-dev-team-local-pilot-smoke/targets/disposable-sandbox-project`
- backup dir:
  `/private/tmp/ai-dev-team-local-pilot-smoke/backups/`
- Web Office bind:
  `127.0.0.1:8001`

The disposable target repo was initialized locally with `git init` and was not
attached to the live Docker-based main project.

## Verified Surfaces

### Web Office startup

Verified:

- Web Office started locally with an isolated `STATE_DB_PATH`
- local bind target was `127.0.0.1:8001`
- within this Codex session the local bind required sandbox approval; that is a
  tooling constraint of the session, not a product/runtime blocker

### `/healthz`

Verified response:

```json
{"ok":true,"app":"AI Dev Team Web Office API","schema_version":11,"project_registry_ready":true,"state_db_path":"/private/tmp/ai-dev-team-local-pilot-smoke/state/state.db","state_db_fallback_in_use":false}
```

### `/readyz`

Verified response:

```json
{"ok":true,"ready":true,"app":"AI Dev Team Web Office API","schema_version":11,"project_registry_ready":true,"state_db_path":"/private/tmp/ai-dev-team-local-pilot-smoke/state/state.db","state_db_fallback_in_use":false}
```

### Local verified backup

Verified command result:

```json
{"backup_path":"/private/tmp/ai-dev-team-local-pilot-smoke/backups/state-db-20260518T052930Z.sqlite3","created_at_utc":"2026-05-18T05:29:30Z","manifest_path":"/private/tmp/ai-dev-team-local-pilot-smoke/backups/state-db-20260518T052930Z.json","schema_version":11,"sha256":"76427386c9fdd40c9f8802b692db2bfbc0c571aca78e45091208afcf6f1249fe","size_bytes":172032,"source_state_db_path":"/private/tmp/ai-dev-team-local-pilot-smoke/state/state.db","verification_detail":"Backup artifact verified successfully.","verified":true}
```

## Blocked Surface

### Telegram runtime startup

Truthful blocker:

- no dedicated isolated pilot `TELEGRAM_OWNER_CHAT_ID`
- no dedicated isolated pilot `TELEGRAM_BOT_TOKEN`
- no dedicated isolated pilot `TELEGRAM_AGENT_TOKENS`

Verified startup failure:

```text
Startup config validation failed:
[error] bot.missing_telegram_owner_chat_id: TELEGRAM_OWNER_CHAT_ID is required for bot startup.
[error] bot.missing_bot_identity_startup_path: Bot startup needs TELEGRAM_BOT_TOKEN for single-bot compatibility or TELEGRAM_AGENT_TOKENS for multi-bot startup.
```

This means the local isolated contour is partially verified for Web Office,
health, and backup, but not yet fully ready for a truthful Telegram pilot run
until dedicated local pilot credentials are provided.

## What Was Intentionally Not Done

This step did **not**:

- use Docker
- attach to the live Docker-mounted main project
- enable assist-mode against the main project
- start any VPS/bootstrap/server work
- configure `nginx`, `systemd`, domain, or HTTPS
- claim successful Telegram pilot startup without dedicated isolated credentials
