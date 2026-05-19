# L0.16 — live activation proof for `devops_agent`

## Step scope

This step completes the second specialist promotion pattern that was opened in
`L0.15`:

- `devops_agent` remains a specialist role
- `devops_agent` does not become a baseline internal team member
- `devops_agent` is allowed as an optional live Telegram identity only through
  explicit local env wiring
- this step is only about the real local token path, fifth-bot startup, and
  one bounded personal direct DM proof

This step does **not**:

- activate `data_agent`
- open a broad all-specialists wave
- jump to 20–30 live bots
- attach Hedgekeeper
- enable write-assisted main-project work
- start VPS/prod rollout
- refactor the runtime contract again

## Exact local env/token contract

`.env.example` truthfully documents:

- `TELEGRAM_DEVOPS_BOT_TOKEN`
- example promoted mapping:
  `devops_agent=TELEGRAM_DEVOPS_BOT_TOKEN`
- example five-bot multi-identity mapping:
  `TELEGRAM_AGENT_TOKENS=coordinator_agent=TELEGRAM_BOT_TOKEN,writer_agent=TELEGRAM_WRITER_BOT_TOKEN,reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN,security_agent=TELEGRAM_SECURITY_BOT_TOKEN,devops_agent=TELEGRAM_DEVOPS_BOT_TOKEN`

Actual local env truth on this Mac during `L0.16`:

- `TELEGRAM_OWNER_CHAT_ID_present=true`
- `TELEGRAM_BOT_TOKEN_present=true`
- `TELEGRAM_WRITER_BOT_TOKEN_present=true`
- `TELEGRAM_REVIEWER_BOT_TOKEN_present=true`
- `TELEGRAM_SECURITY_BOT_TOKEN_present=true`
- `TELEGRAM_DEVOPS_BOT_TOKEN_present=true`
- `TELEGRAM_DEVOPS_BOT_TOKEN_len=46`
- actual local `TELEGRAM_AGENT_TOKENS` equals:
  `coordinator_agent=TELEGRAM_BOT_TOKEN,writer_agent=TELEGRAM_WRITER_BOT_TOKEN,reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN,security_agent=TELEGRAM_SECURITY_BOT_TOKEN,devops_agent=TELEGRAM_DEVOPS_BOT_TOKEN`

## Exact runtime path actually used

This live proof used the same isolated live persistence paths as the accepted
local Telegram contour:

- `STATE_DB_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/state/state.db`
- `OBS_LOG_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/logs/pipeline-log.jsonl`

Runtime path used:

1. load the real local `.env`
2. build the multi-bot runtime spec through `core.bot_runner.build_multi_bot_runtime_spec_from_env(...)`
3. confirm the assembled role order:
   `('coordinator_agent', 'devops_agent', 'reviewer_agent', 'security_agent', 'writer_agent')`
4. start the real Telegram runtime:
   `.venv/bin/python scripts/run_telegram_bot.py --log-level INFO`
5. wait for live Telegram reachability, PTB startup, and polling confirmation
6. send one bounded direct DM command to the live `devops_agent` identity

## Exact startup/reachability facts

Live startup succeeded for the widened 5-bot contour:

- `coordinator_agent`
  - bot username: `@ai_dev_team_lead_bot`
  - token_valid=`true`
  - reachable=`true`
  - started=`true`
  - polling_started=`true`
- `devops_agent`
  - bot username: `@ai_dev_team_dev_ops_bot`
  - token_valid=`true`
  - reachable=`true`
  - started=`true`
  - polling_started=`true`
- `reviewer_agent`
  - bot username: `@ai_dev_team_reviewer_bot`
  - token_valid=`true`
  - reachable=`true`
  - started=`true`
  - polling_started=`true`
- `security_agent`
  - bot username: `@ai_dev_team_security_agent_bot`
  - token_valid=`true`
  - reachable=`true`
  - started=`true`
  - polling_started=`true`
- `writer_agent`
  - bot username: `@ai_dev_team_writer_bot`
  - token_valid=`true`
  - reachable=`true`
  - started=`true`
  - polling_started=`true`

This resolves the exact `L0.15` blocker:

- the local `TELEGRAM_DEVOPS_BOT_TOKEN` now exists
- the real runtime now accepts and starts `devops_agent`
- the fifth live bot is no longer just contract-opened; it is live

## Exact direct personal DM-proof

Direct personal DM proof used:

- inbound message: `/help`
- target identity: `@ai_dev_team_dev_ops_bot`
- exact observed reply prefix:
  `Девопс: 🛠 Доступные команды`

Observed reply body then listed the command surface:

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

This is a truthful personal direct DM proof because:

- the inbound message went directly to the live `devops_agent` bot identity
- the reply was visibly branded as `Девопс:`
- the response did not fall back to `Координатор:`
- this is direct live identity proof, not only a logical pipeline mention

The personal direct-DM voice behavior remains aligned with
`docs/LOCAL_DIRECT_DM_ROLE_VOICE.md`.

## Operator-visible aftermath

The widened live contour after `L0.16` is now:

- `coordinator_agent` → `@ai_dev_team_lead_bot`
- `writer_agent` → `@ai_dev_team_writer_bot`
- `reviewer_agent` → `@ai_dev_team_reviewer_bot`
- `security_agent` → `@ai_dev_team_security_agent_bot`
- `devops_agent` → `@ai_dev_team_dev_ops_bot`

Operator-surface readback against the same live `STATE_DB_PATH` remained
truthful:

- `/healthz` → `ok=true`, `state_db_fallback_in_use=false`
- `/readyz` → `ready=true`, `state_db_fallback_in_use=false`
- `/api/projects/sandbox_project/history` → `count=3`
- `/api/projects/sandbox_project/threads` → `count=3`
- `project_task_count=3`
- `thread_count=3`
- latest persisted task still remains `task-1779122095-e24170`

The bounded `/help` DM proof did **not** create fake task/thread artifacts.

## Roster impact

Current roster impact after truthful `L0.16` activation:

- live identities before = `4`
- live identities after = `5`
- `coordinator_agent` remains live
- `writer_agent` remains live
- `reviewer_agent` remains live
- `security_agent` remains live
- `devops_agent` is now live on this machine
- `devops_agent` still remains a specialist role, not a baseline team member

## What remains intentionally not done

- `data_agent` is still not activated
- all specialist roles are still not live
- 20–30 live agents are still not running
- Hedgekeeper is still not attached
- write-assisted main-project work is still not enabled
- VPS/prod rollout is still separate

## Outcome

Outcome: `devops_agent live identity certified`

## Handoff to next step

The second specialist promotion pattern is now proven end-to-end:

1. `security_agent` is live and personal
2. `devops_agent` is live and personal
3. the next bounded expansion step, if chosen, should stay narrow again:
   one specialist at a time rather than a broad all-specialists wave
