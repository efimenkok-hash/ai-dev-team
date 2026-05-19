# L0.17 — contract-opened live identity attempt for `data_agent`

## Step scope

This step closes the current specialist catalog by promoting the last remaining
specialist role into the optional live-identity contract:

- `data_agent` remains a specialist role
- `data_agent` does not become a baseline internal team member
- `data_agent` may become an optional live Telegram identity only through
  explicit local env wiring
- this step is only about the third narrow specialist promotion contract, the
  local token path, one truthful live activation attempt, and exact blocker
  recording if the token is absent

This step does **not**:

- open any future roles outside the current catalog
- jump to 20–30 live bots
- attach Hedgekeeper
- enable write-assisted main-project work
- start VPS/prod rollout
- redesign direct-DM voice semantics again
- introduce any broad runtime refactor

## Exact runtime contract for `data_agent`

The runtime contract after `L0.17` is:

- `security_agent` remains a promoted optional live specialist identity
- `devops_agent` remains a promoted optional live specialist identity
- `data_agent` is now also allowed as a promoted optional live specialist
  identity
- none of these specialist roles becomes a baseline internal team member

Truthful boundary:

- the current specialist catalog is now fully runtime-exposed by contract:
  `security_agent`, `devops_agent`, `data_agent`
- actual live activation still requires explicit mapping in
  `TELEGRAM_AGENT_TOKENS`
- `data_agent` is contract-opened here, but not live-certified on this Mac
  unless `TELEGRAM_DATA_BOT_TOKEN` exists locally

## Exact local env/token contract

`.env.example` now truthfully documents:

- `TELEGRAM_DATA_BOT_TOKEN`
- example promoted mapping:
  `data_agent=TELEGRAM_DATA_BOT_TOKEN`
- example six-bot multi-identity mapping:
  `TELEGRAM_AGENT_TOKENS=coordinator_agent=TELEGRAM_BOT_TOKEN,writer_agent=TELEGRAM_WRITER_BOT_TOKEN,reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN,security_agent=TELEGRAM_SECURITY_BOT_TOKEN,devops_agent=TELEGRAM_DEVOPS_BOT_TOKEN,data_agent=TELEGRAM_DATA_BOT_TOKEN`

Actual local env truth on this Mac during `L0.17`:

- `TELEGRAM_OWNER_CHAT_ID_present=true`
- `TELEGRAM_BOT_TOKEN_present=true`
- `TELEGRAM_WRITER_BOT_TOKEN_present=true`
- `TELEGRAM_REVIEWER_BOT_TOKEN_present=true`
- `TELEGRAM_SECURITY_BOT_TOKEN_present=true`
- `TELEGRAM_DEVOPS_BOT_TOKEN_present=true`
- `TELEGRAM_DATA_BOT_TOKEN_present=false`
- `TELEGRAM_DATA_BOT_TOKEN_len=0`
- actual local `TELEGRAM_AGENT_TOKENS` still equals:
  `coordinator_agent=TELEGRAM_BOT_TOKEN,writer_agent=TELEGRAM_WRITER_BOT_TOKEN,reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN,security_agent=TELEGRAM_SECURITY_BOT_TOKEN,devops_agent=TELEGRAM_DEVOPS_BOT_TOKEN`

## Exact runtime path actually used

`L0.17` used four bounded truth sources:

1. update the runtime-exposed catalog so `data_agent` is allowed by contract
2. verify that the real current local `.env` still resolves only to the
   accepted 5-role live contour:
   `('coordinator_agent', 'devops_agent', 'reviewer_agent', 'security_agent', 'writer_agent')`
3. perform one minimal truthful activation attempt by extending
   `TELEGRAM_AGENT_TOKENS` in-memory with:
   `data_agent=TELEGRAM_DATA_BOT_TOKEN`
4. stop on the exact blocker:
   `ValueError: telegram_agent_token_env_missing:TELEGRAM_DATA_BOT_TOKEN`

This step did **not** pretend that a sixth live identity started without a
real local token.

## Exact startup/reachability facts or exact blocker

Current real 5-bot contour remains healthy on this Mac:

- `coordinator_agent`
  - bot username: `@ai_dev_team_lead_bot`
  - still live from the accepted contour
- `devops_agent`
  - bot username: `@ai_dev_team_dev_ops_bot`
  - still live from the accepted contour
- `reviewer_agent`
  - bot username: `@ai_dev_team_reviewer_bot`
  - still live from the accepted contour
- `security_agent`
  - bot username: `@ai_dev_team_security_agent_bot`
  - still live from the accepted contour
- `writer_agent`
  - bot username: `@ai_dev_team_writer_bot`
  - still live from the accepted contour

Exact `data_agent` blocker:

- `data_agent` is now allowed by the runtime contract
- but the actual local env still has no `TELEGRAM_DATA_BOT_TOKEN`
- the first truthful activation attempt now stops with:
  `ValueError: telegram_agent_token_env_missing:TELEGRAM_DATA_BOT_TOKEN`

Because of that blocker, there is still no truthful live proof yet for:

- `token_valid` on `data_agent`
- `reachable` on `data_agent`
- `started` on `data_agent`
- `polling_started` on `data_agent`
- a real `getMe` identity check for `data_agent`
- a truthful bot username claim for a live `data_agent` bot on this Mac

## Exact direct personal DM-proof

There is no truthful direct personal DM-proof yet for `data_agent`.

This step therefore does **not** claim:

- a real direct DM to a live `data_agent` bot identity
- a real observed reply from `data_agent`
- a widened live roster from `5` to `6`

The blocker is bounded and exact:

- direct personal DM-proof is blocked only because the actual local
  `TELEGRAM_DATA_BOT_TOKEN` is still missing
- the direct-DM voice path itself is already solved generically by
  `docs/LOCAL_DIRECT_DM_ROLE_VOICE.md`
- the expected role voice, once live, is `Дата-инженер:`

## Operator-visible aftermath

Current operator-visible truth after the blocked live attempt:

- existing live contour remains:
  - `coordinator_agent` → `@ai_dev_team_lead_bot`
  - `writer_agent` → `@ai_dev_team_writer_bot`
  - `reviewer_agent` → `@ai_dev_team_reviewer_bot`
  - `security_agent` → `@ai_dev_team_security_agent_bot`
  - `devops_agent` → `@ai_dev_team_dev_ops_bot`
- no fake `data_agent` tasks were created
- no fake `data_agent` threads were created
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

Current roster impact after truthful `L0.17` blocked activation:

- live identities before = `5`
- live identities after = `5`
- `coordinator_agent` remains live
- `writer_agent` remains live
- `reviewer_agent` remains live
- `security_agent` remains live
- `devops_agent` remains live
- `data_agent` is now allowed by contract but not yet live on this machine
- `data_agent` still remains a specialist role, not a baseline team member

## What remains intentionally not done

- 20–30 live agents are still not running
- any broader assistant wave is still separate
- Hedgekeeper is still not attached
- write-assisted main-project work is still not enabled
- VPS/prod rollout is still separate

## Outcome

Outcome: `data_agent live identity partially blocked`

## Handoff to next step

To move this exact step from contract-opened blocked-path to live-proof:

1. add a real local `TELEGRAM_DATA_BOT_TOKEN`
2. extend the real local `TELEGRAM_AGENT_TOKENS` with
   `data_agent=TELEGRAM_DATA_BOT_TOKEN`
3. restart the live local Telegram runtime
4. verify:
   - Telegram `getMe`
   - startup
   - polling
   - one bounded direct DM proof against the live `data_agent` identity
