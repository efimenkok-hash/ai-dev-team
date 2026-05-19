# L0.15 — second specialist live-identity attempt for `devops_agent`

## Step scope

This step applies the already-proven specialist promotion pattern to the next
bounded target:

- `devops_agent` remains a specialist role
- `devops_agent` does not become a baseline internal team member
- `devops_agent` may become an optional live Telegram identity only through
  explicit local env wiring
- this step is only about the second narrow specialist promotion contract,
  the local token path, live startup feasibility, and one bounded direct DM
  proof if the token exists

This step does **not**:

- activate `data_agent`
- open a broad all-specialists wave
- jump to 20–30 live bots
- attach Hedgekeeper
- enable write-assisted main-project work
- start VPS/prod rollout
- change direct-DM role voice semantics again
- introduce any broad orchestration or UI refactor

## Exact runtime contract for `devops_agent`

The runtime contract after `L0.15` is:

- `security_agent` remains a promoted optional live specialist identity
- `devops_agent` is now also allowed as a promoted optional live specialist
  identity
- `data_agent` still remains closed as a live identity on this step
- neither `security_agent` nor `devops_agent` becomes a baseline internal team
  member

Truthful boundary:

- runtime-exposed catalog now includes:
  - baseline internal team
  - `security_agent`
  - `devops_agent`
- actual live activation still requires explicit mapping in
  `TELEGRAM_AGENT_TOKENS`

## Exact env/token contract

`.env.example` now truthfully documents:

- `TELEGRAM_DEVOPS_BOT_TOKEN`
- example promoted mapping:
  `devops_agent=TELEGRAM_DEVOPS_BOT_TOKEN`
- example second-specialist multi-bot mapping:
  `TELEGRAM_AGENT_TOKENS=coordinator_agent=TELEGRAM_BOT_TOKEN,writer_agent=TELEGRAM_WRITER_BOT_TOKEN,reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN,security_agent=TELEGRAM_SECURITY_BOT_TOKEN,devops_agent=TELEGRAM_DEVOPS_BOT_TOKEN`

Actual local env truth on this Mac during `L0.15`:

- `TELEGRAM_OWNER_CHAT_ID_present=true`
- `TELEGRAM_BOT_TOKEN_present=true`
- `TELEGRAM_WRITER_BOT_TOKEN_present=true`
- `TELEGRAM_REVIEWER_BOT_TOKEN_present=true`
- `TELEGRAM_SECURITY_BOT_TOKEN_present=true`
- `TELEGRAM_DEVOPS_BOT_TOKEN_present=false`
- `TELEGRAM_DEVOPS_BOT_TOKEN_len=0`
- actual local `TELEGRAM_AGENT_TOKENS` still equals:
  `coordinator_agent=TELEGRAM_BOT_TOKEN,writer_agent=TELEGRAM_WRITER_BOT_TOKEN,reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN,security_agent=TELEGRAM_SECURITY_BOT_TOKEN`

Important truthfulness note:

- there is no stray `.env.save` snapshot in the repo root anymore
- the blocker is no longer secret-sprawl ambiguity
- the blocker is simply that the real local runtime env still has no
  `TELEGRAM_DEVOPS_BOT_TOKEN`

## Exact runtime path used

`L0.15` used three bounded truth sources:

1. update the runtime-exposed catalog so `devops_agent` is allowed by contract
2. verify the actual local `.env` still resolves only to the current 4-role
   live contour:
   `('coordinator_agent', 'reviewer_agent', 'security_agent', 'writer_agent')`
3. perform one minimal truthful activation attempt by extending
   `TELEGRAM_AGENT_TOKENS` in-memory with:
   `devops_agent=TELEGRAM_DEVOPS_BOT_TOKEN`
4. stop on the exact blocker:
   `ValueError: telegram_agent_token_env_missing:TELEGRAM_DEVOPS_BOT_TOKEN`
5. restart the real local Telegram runtime on the patched code with the same
   isolated live DB/log path to verify that the current 4-bot contour still
   stays healthy without the missing devops token

Patched runtime restart path used for non-regression:

- `STATE_DB_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/state/state.db`
- `OBS_LOG_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/logs/pipeline-log.jsonl`
- `.venv/bin/python scripts/run_telegram_bot.py --log-level INFO`

## Exact startup/reachability proof or exact blocker

Current real 4-bot contour remained healthy on the patched code:

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

Exact `devops_agent` blocker:

- `devops_agent` is now allowed by the runtime contract
- but the actual local env still has no `TELEGRAM_DEVOPS_BOT_TOKEN`
- the first truthful activation attempt stops with:
  `ValueError: telegram_agent_token_env_missing:TELEGRAM_DEVOPS_BOT_TOKEN`

Because of that blocker, there is still no truthful live proof yet for:

- `token_valid` on `devops_agent`
- `reachable` on `devops_agent`
- `started` on `devops_agent`
- `polling_started` on `devops_agent`
- a real `getMe` identity check for `devops_agent`
- a truthful bot username claim for a live `devops_agent` bot on this Mac

## Exact direct DM proof

There is no truthful direct DM proof yet for `devops_agent`.

This step therefore does **not** claim:

- a real direct DM to a live `devops_agent` bot identity
- a real observed reply from `devops_agent`
- a widened live roster from `4` to `5`

The blocker is bounded and exact:

- direct personal DM proof is blocked only because the actual local
  `TELEGRAM_DEVOPS_BOT_TOKEN` is still missing
- the direct-DM voice path itself is already solved generically by
  `docs/LOCAL_DIRECT_DM_ROLE_VOICE.md`

## Operator-visible aftermath

Current operator-visible truth after the blocked live attempt:

- existing live contour remains:
  - `coordinator_agent` → `@ai_dev_team_lead_bot`
  - `writer_agent` → `@ai_dev_team_writer_bot`
  - `reviewer_agent` → `@ai_dev_team_reviewer_bot`
  - `security_agent` → `@ai_dev_team_security_agent_bot`
- no fake `devops_agent` tasks were created
- no fake `devops_agent` threads were created
- no fake roster widening was persisted

Operator-surface readback against the same live `STATE_DB_PATH` remained
truthful:

- `/healthz` → `ok=true`, `state_db_fallback_in_use=false`
- `/readyz` → `ready=true`, `state_db_fallback_in_use=false`
- `/api/projects/sandbox_project/history` → `count=3`
- `/api/projects/sandbox_project/threads` → `count=3`
- `project_task_count=3`
- `thread_count=3`
- latest persisted task still remains `task-1779122095-e24170`

## Roster impact

Current roster impact after truthful `L0.15` blocked activation:

- live identities before = `4`
- live identities after = `4`
- `coordinator_agent` remains live
- `writer_agent` remains live
- `reviewer_agent` remains live
- `security_agent` remains live
- `devops_agent` is now allowed by contract but not yet live on this machine
- `devops_agent` still remains a specialist role, not a baseline team member

## What remains intentionally not done

- `data_agent` is still not activated
- all specialist roles are still not live
- 20–30 live agents are still not running
- Hedgekeeper is still not attached
- write-assisted main-project work is still not enabled
- VPS/prod rollout is still separate

## Outcome

Outcome: `devops_agent live identity partially blocked`

The blocker is exact and bounded:

- the runtime contract is now correct
- the docs/tests are now correct
- the direct-DM voice contract is already solved
- the only missing piece for real activation on this Mac is the absent local
  `TELEGRAM_DEVOPS_BOT_TOKEN`

## Handoff to next step

To move this exact step from blocked-path to live-proof:

1. add a real local `TELEGRAM_DEVOPS_BOT_TOKEN`
2. extend the real local `TELEGRAM_AGENT_TOKENS` with
   `devops_agent=TELEGRAM_DEVOPS_BOT_TOKEN`
3. restart the live local Telegram runtime
4. verify:
   - Telegram `getMe`
   - startup
   - polling
   - one bounded direct DM proof against the live `devops_agent` identity
