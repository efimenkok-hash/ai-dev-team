# Local Isolated Pilot Runbook

Checked and authored on `2026-05-18`.

This document is the canonical operator-facing runbook for the isolated local
pilot track. It exists before any VPS bootstrap and stays deliberately separate
from the Docker-based live main project path.

Use this runbook when you want to:

- raise AI Dev Team locally without Docker
- verify Web Office, `/healthz`, `/readyz`, and local backup behavior
- prepare a safe disposable pilot repo target
- certify the local contour before any `C1.*` server rollout

Do not use this runbook as a production deploy guide. It is local-first,
native-first, and intentionally isolated from the live main project.

## Why This Local Path Exists

The local pilot track exists to give the operator a safe native bootstrap path
before:

- buying or bootstrapping a VPS
- attaching AI Dev Team directly to the live Docker-mounted main project
- turning on server-only concerns such as `systemd`, `nginx`, domain wiring,
  HTTPS, remote backups, or server-side health monitoring

This local path is intentionally:

- native local first
- no Docker
- no Podman
- no dependency on the live Docker control plane of the main project

Optional later variant:

- an isolated Ubuntu VM can be used later if macOS-native pilot constraints
  become a problem

For `L0.1`, the preferred path is still native local on macOS.

## Canonical Isolation Rules

The canonical operator path for the isolated local pilot is:

- pilot root: `~/ai-dev-team-local-pilot/`
- dedicated env file: `~/ai-dev-team-local-pilot/local-pilot.env`
- dedicated state path: `~/ai-dev-team-local-pilot/state/state.db`
- dedicated worktree root: `~/ai-dev-team-local-pilot/worktrees/`
- dedicated observability log path:
  `~/ai-dev-team-local-pilot/logs/pipeline-log.jsonl`
- dedicated target repo root: `~/ai-dev-team-local-pilot/targets/`
- dedicated backup target:
  `~/ai-dev-team-local-pilot/backups/`
- dedicated Web Office bind:
  `127.0.0.1:8001`

Isolation rules are strict:

1. Always use an explicit `STATE_DB_PATH`.
2. Always use an explicit `WORKTREE_ROOT`.
3. Keep logs under the local pilot root, not in the live project’s paths.
4. Keep target repositories under the local pilot `targets/` directory.
5. Keep Web Office on `127.0.0.1:8001`, not on the default `8000`, to avoid
   collisions with other local services.
6. Prefer leaving `BOT_STATE_DIR` unset in this pilot path so the runtime uses
   the explicit canonical `STATE_DB_PATH` rather than the legacy fallback.

## Main Project Safety Rule

Do not point `REPO_PATH` at the live Docker-mounted main project path.

The first local pilot must use one of these:

- a disposable sandbox repo under
  `~/ai-dev-team-local-pilot/targets/disposable-sandbox-project`
- a separate clean clone of the private main repo under
  `~/ai-dev-team-local-pilot/targets/<clean-clone-name>`

What is explicitly forbidden at `L0.1`:

- direct use of the live Docker-mounted main project working tree
- shared worktrees with the live main project agent setup
- direct assist-mode attachment to the live main project

## Preferred Local Layout

Prepare the local pilot root:

```bash
mkdir -p \
  ~/ai-dev-team-local-pilot/state \
  ~/ai-dev-team-local-pilot/worktrees \
  ~/ai-dev-team-local-pilot/logs \
  ~/ai-dev-team-local-pilot/targets \
  ~/ai-dev-team-local-pilot/backups
```

Prepare a disposable target repo:

```bash
mkdir -p ~/ai-dev-team-local-pilot/targets/disposable-sandbox-project
cd ~/ai-dev-team-local-pilot/targets/disposable-sandbox-project
git init
```

Prepare a dedicated env file outside the repository checkout:

```dotenv
# ~/ai-dev-team-local-pilot/local-pilot.env
STATE_DB_PATH=~/ai-dev-team-local-pilot/state/state.db
WORKTREE_ROOT=~/ai-dev-team-local-pilot/worktrees
OBS_LOG_PATH=~/ai-dev-team-local-pilot/logs/pipeline-log.jsonl
REPO_PATH=~/ai-dev-team-local-pilot/targets/disposable-sandbox-project
LOG_LEVEL=INFO

# Add only when you are ready to validate Telegram startup for the pilot:
# TELEGRAM_OWNER_CHAT_ID=123456789
# TELEGRAM_BOT_TOKEN=1234567890:replace-me
# OPENROUTER_API_KEY=sk-or-v1-replace-me
# OPENAI_API_KEY=sk-replace-me
```

The dedicated env file is intentionally outside the repo so local pilot values
override any existing repository `.env` without editing it.

## Local Commands

### 1. Python environment

From the AI Dev Team repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

### 2. Load the isolated pilot env

```bash
set -a
source ~/ai-dev-team-local-pilot/local-pilot.env
set +a
```

### 3. Run the Telegram runtime locally

```bash
.venv/bin/python scripts/run_telegram_bot.py --log-level INFO
```

What to expect:

- startup config validation runs before deep runtime startup
- if `TELEGRAM_OWNER_CHAT_ID` or bot tokens are absent, startup fails fast with
  a deterministic validation error
- this is still truthful and acceptable for `L0.1` if the blocker is recorded

### 4. Run the Web Office locally

```bash
.venv/bin/python -m uvicorn web.main:app --host 127.0.0.1 --port 8001
```

Then verify:

- Dashboard: [http://127.0.0.1:8001/](http://127.0.0.1:8001/)
- health: [http://127.0.0.1:8001/healthz](http://127.0.0.1:8001/healthz)
- readiness: [http://127.0.0.1:8001/readyz](http://127.0.0.1:8001/readyz)

### 5. Check local health surfaces

```bash
curl -sS http://127.0.0.1:8001/healthz
curl -sS http://127.0.0.1:8001/readyz
```

Expected truth:

- `state_db_path` points at the isolated pilot path
- `state_db_fallback_in_use` is `false`
- `project_registry_ready` is `true`

### 6. Create a verified local backup

```bash
.venv/bin/python scripts/backup_state_db.py \
  --backup-dir ~/ai-dev-team-local-pilot/backups
```

Expected truth:

- backup artifact is created under the isolated backup directory
- manifest sidecar is created next to it
- `verified` is `true`

## What This Step Still Does Not Do

This local pilot runbook does **not** do the following:

- no Docker
- no Podman
- no VPS bootstrap
- no production `systemd`
- no production `nginx`
- no domain / HTTPS
- no remote backup automation
- no direct attach to the live Docker-mounted main project
- no automatic assist-mode against the main live project

## Local Pilot Track

The local deployment track before live project attach is:

- `L0.1` — isolated local pilot docs + bootstrap baseline
- `L0.2` — local team / agent certification
- `L0.3` — local Web Office / UI certification
- `L0.4` — local pilot task on a sandbox repo
- `L0.5` — safe attach to the main project in assist-mode
