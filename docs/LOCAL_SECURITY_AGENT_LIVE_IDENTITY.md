# L0.13 — live direct-DM certification for `security_agent`

## Step scope

This step closes the first promoted specialist activation:

- `security_agent` remains a specialist role
- `security_agent` does not become a baseline internal team member
- `security_agent` is allowed as an optional live Telegram identity only
  through explicit local env wiring
- this step is only about the real local secret path, live runtime startup,
  reachability, polling, and one bounded direct DM proof

This step does **not**:

- activate `devops_agent`
- activate `data_agent`
- open a broader specialist wave
- jump to 20–30 live bots
- attach Hedgekeeper
- enable write-assisted main-project work
- start VPS/prod rollout
- introduce any broad UI or orchestration refactor

## Exact local env/token contract

The runtime/doc contract from `L0.11-L0.12` remains valid:

- `security_agent` is an optional live Telegram identity
- it is enabled only when `TELEGRAM_AGENT_TOKENS` explicitly maps
  `security_agent=TELEGRAM_SECURITY_BOT_TOKEN`
- the absence of that mapping must leave the current contour unchanged

`.env.example` still truthfully documents the required contract:

- `TELEGRAM_SECURITY_BOT_TOKEN`
- example:
  `TELEGRAM_AGENT_TOKENS=coordinator_agent=TELEGRAM_BOT_TOKEN,writer_agent=TELEGRAM_WRITER_BOT_TOKEN,reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN,security_agent=TELEGRAM_SECURITY_BOT_TOKEN`

Actual local env truth on this Mac during accepted `L0.13` work:

- `TELEGRAM_OWNER_CHAT_ID_present=true`
- `TELEGRAM_BOT_TOKEN_present=true`
- `TELEGRAM_WRITER_BOT_TOKEN_present=true`
- `TELEGRAM_REVIEWER_BOT_TOKEN_present=true`
- `TELEGRAM_SECURITY_BOT_TOKEN_present=true`
- `TELEGRAM_SECURITY_BOT_TOKEN_len=46`
- actual local `TELEGRAM_AGENT_TOKENS` equals:
  `coordinator_agent=TELEGRAM_BOT_TOKEN,writer_agent=TELEGRAM_WRITER_BOT_TOKEN,reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN,security_agent=TELEGRAM_SECURITY_BOT_TOKEN`

Important truthfulness note:

- the token did already exist locally in a saved env snapshot before this step
- `L0.13` converted that from a saved local secret snapshot into the actual
  runtime env path by merging the required values into the real local `.env`
- the stray repo-root `.env.save` file was then removed from the workspace root
  so the secret no longer sat in an untracked non-runtime snapshot there

## Exact runtime path actually used

`L0.13` used the real local contour and the same isolated live DB/log path that
already backed the certified local Telegram contour:

1. merge the saved local `TELEGRAM_SECURITY_BOT_TOKEN` and
   `TELEGRAM_AGENT_TOKENS` values into the actual local `.env`
2. confirm that `.env.save` is no longer present at the repo root
3. verify the real local env resolves to a 4-role runtime contract:
   `('coordinator_agent', 'reviewer_agent', 'security_agent', 'writer_agent')`
4. stop the stale 3-bot runtime process
5. restart the real Telegram runtime on the same isolated live state/log paths:
   `STATE_DB_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/state/state.db`
   `OBS_LOG_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/logs/pipeline-log.jsonl`
   `.venv/bin/python scripts/run_telegram_bot.py --log-level INFO`
6. allow the runtime to perform real Telegram reachability probes and PTB
   startup outside the sandboxed transport restriction boundary
7. verify one bounded direct DM proof against the live
   `@ai_dev_team_security_agent_bot` identity

## Exact startup/reachability facts

Live runtime proof on the restarted 4-bot contour:

- `coordinator_agent`
  - bot username: `@ai_dev_team_lead_bot`
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

This is the exact capability boundary that changed between the old blocked path
and the truthful `L0.13` state:

- `security_agent` is no longer only allowed-by-contract
- `security_agent` is now part of the real live Telegram runtime on this Mac

## Exact direct DM proof

Bounded live direct proof that actually happened:

- direct target chat: `@ai_dev_team_security_agent_bot`
- exact inbound message: `/help`
- exact observed reply prefix:
  `Координатор: 🛠 Доступные команды`

Truthfully, this proves:

- the owner wrote directly to the live `security_agent` bot identity
- the message reached that live bot
- the runtime produced a reply through that same live identity path
- this is a real direct Telegram proof, not a logical pipeline-only mention

Truthfully, this does **not** prove the final desired DM UX yet:

- the reply body for slash-commands is still coordinator-branded
- current bridge command flow signs slash-command replies through
  `Координатор:` even when the inbound DM arrived via a secondary live bot
- that is a separate product/UX gap, not evidence that
  `security_agent` activation failed

## Operator-visible aftermath

The current contour widened without regressing the already-working local office:

- direct live identity surfaced:
  `security_agent` → `@ai_dev_team_security_agent_bot`
- current 3-bot contour remained healthy:
  - `coordinator_agent` → `@ai_dev_team_lead_bot`
  - `writer_agent` → `@ai_dev_team_writer_bot`
  - `reviewer_agent` → `@ai_dev_team_reviewer_bot`
- the `/help` direct DM proof did **not** create fake project tasks or threads

Non-regression checks against the same isolated live DB remained truthful:

- `/healthz` → `ok=true`, `state_db_fallback_in_use=false`
- `/readyz` → `ready=true`
- `/api/projects/sandbox_project/history` → `count=3`
- `/api/projects/sandbox_project/threads` → `count=3`
- latest persisted task still remains `task-1779122095-e24170`

## Roster impact

Current roster impact after truthful `L0.13` activation:

- live identities before = `3`
- live identities after = `4`
- `coordinator_agent` remains live
- `writer_agent` remains live
- `reviewer_agent` remains live
- `security_agent` is now live
- `security_agent` still remains a specialist role, not a baseline team member

Live-vs-logical boundary after this step:

- separate live Telegram identities now include:
  `coordinator_agent`, `writer_agent`, `reviewer_agent`, `security_agent`
- broader logical pipeline roles still remain wider than the live identity set

## What remains intentionally not done

- `devops_agent` is still not activated
- `data_agent` is still not activated
- all specialist roles are still not live
- 20–30 live agents are still not running
- Hedgekeeper is still not attached
- write-assisted main-project work is still not enabled
- VPS/prod rollout is still separate

## Outcome

Outcome: `security_agent live identity certified`

Truthful caveat:

- live activation is certified
- startup/reachability/polling are certified
- one direct DM proof is certified
- role-specific slash-command voice/signature is **not** yet the final desired
  UX because `/help` still answers with a coordinator-branded signature

Historical note:

- that remaining direct-DM role-voice gap was later closed in `L0.14`
- canonical follow-up artifact:
  `docs/LOCAL_DIRECT_DM_ROLE_VOICE.md`

## Handoff to next step

The correct next narrow step after `L0.13` is **not** another activation step.
The remaining gap is now different:

1. keep `security_agent` live
2. preserve the healthy 4-bot contour
3. fix the direct-DM command signature/voice path so that a direct DM to a
   promoted specialist bot answers in that bot's own role voice instead of
   defaulting to `Координатор:`
