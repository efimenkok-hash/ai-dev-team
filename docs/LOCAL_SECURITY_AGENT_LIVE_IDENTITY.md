# L0.12 — live activation proof for `security_agent`

## Step scope

This step is only about the first real live activation attempt for the already
promoted specialist identity:

- `security_agent` remains a specialist role
- `security_agent` does not become a baseline internal team member
- `security_agent` may become a fourth live Telegram identity only through a
  real local `TELEGRAM_SECURITY_BOT_TOKEN`
- this step is about real token wiring, startup truth, and a direct DM proof

This step does **not**:

- activate `devops_agent`
- activate `data_agent`
- jump to 20–30 live bots
- attach Hedgekeeper
- enable write-assisted main-project work
- start VPS/prod rollout
- introduce any broad UI or product refactor

## Exact env/token contract

The runtime/doc contract from `L0.11` remains valid:

- `security_agent` is an optional live Telegram identity
- it is enabled only when `TELEGRAM_AGENT_TOKENS` explicitly maps
  `security_agent=TELEGRAM_SECURITY_BOT_TOKEN`
- the absence of that mapping must leave the current contour unchanged

`.env.example` still truthfully documents the required contract:

- `TELEGRAM_SECURITY_BOT_TOKEN`
- example:
  `TELEGRAM_AGENT_TOKENS=coordinator_agent=TELEGRAM_BOT_TOKEN,writer_agent=TELEGRAM_WRITER_BOT_TOKEN,reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN,security_agent=TELEGRAM_SECURITY_BOT_TOKEN`

Actual local env truth on this Mac during `L0.12`:

- `TELEGRAM_OWNER_CHAT_ID_present=true`
- `TELEGRAM_BOT_TOKEN_present=true`
- `TELEGRAM_WRITER_BOT_TOKEN_present=true`
- `TELEGRAM_REVIEWER_BOT_TOKEN_present=true`
- `TELEGRAM_SECURITY_BOT_TOKEN_present=false`
- `TELEGRAM_SECURITY_BOT_TOKEN_len=0`
- current `TELEGRAM_AGENT_TOKENS` still equals:
  `coordinator_agent=TELEGRAM_BOT_TOKEN,writer_agent=TELEGRAM_WRITER_BOT_TOKEN,reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN`

So the local secret/token path needed for a real `security_agent` activation is
still absent.

## Actual live runtime path used

The `L0.12` activation attempt stayed truthful and stopped at the precondition
boundary instead of faking a startup:

1. load the actual local `.env`
2. verify presence/absence of the required Telegram token keys
3. build the current multi-bot spec from the real env
4. confirm that the current real contour still resolves only to:
   `('coordinator_agent', 'reviewer_agent', 'writer_agent')`
5. attempt the minimal truthful activation shape by extending
   `TELEGRAM_AGENT_TOKENS` in-memory with:
   `security_agent=TELEGRAM_SECURITY_BOT_TOKEN`
6. stop on the exact runtime blocker:
   `ValueError: telegram_agent_token_env_missing:TELEGRAM_SECURITY_BOT_TOKEN`

Because the real token path is still absent, there was no truthful basis to
start a new 4-bot PTB runtime, call Telegram `getMe`, or claim polling for a
live `security_agent` bot.

## Exact startup/reachability facts

Current real 3-bot contour facts remained valid:

- `coordinator_agent` is still part of the live contour
- `writer_agent` is still part of the live contour
- `reviewer_agent` is still part of the live contour

Current real `security_agent` activation facts:

- `token_valid` was not verified for `security_agent`
- `reachable` was not verified for `security_agent`
- `started` was not verified for `security_agent`
- `polling_started` was not verified for `security_agent`
- no truthful bot username can be claimed yet for `security_agent`

Exact blocker:

- `TELEGRAM_SECURITY_BOT_TOKEN` is still absent locally
- therefore `security_agent` is still missing from the actual live
  `TELEGRAM_AGENT_TOKENS` contour
- a forced in-memory activation attempt stops with:
  `ValueError: telegram_agent_token_env_missing:TELEGRAM_SECURITY_BOT_TOKEN`

## Exact direct DM proof

There is no truthful direct DM proof yet for the intended
`@ai_dev_team_security_agent_bot` identity.

The required direct role proof could not be performed because:

- no real `security_agent` bot token was available locally
- no real `getMe` proof could be taken
- no real PTB startup/polling could be taken
- no bounded inbound message such as `/help`, greeting, or short security
  question could be sent to a live `security_agent` identity

This step therefore does **not** claim:

- a direct DM to `@ai_dev_team_security_agent_bot`
- a real observed reply from `security_agent`
- a widened live roster from 3 to 4 identities

## Operator-visible aftermath

The current contour remained stable and truthful because activation stopped
before any fake runtime change:

- `coordinator_agent` still maps to `@ai_dev_team_lead_bot`
- `writer_agent` still maps to `@ai_dev_team_writer_bot`
- `reviewer_agent` still maps to `@ai_dev_team_reviewer_bot`
- no fake `security_agent` tasks were created
- no fake `security_agent` threads were created
- no fake roster widening was persisted

Non-regression checks against the same isolated live local DB still returned:

- `/healthz` → `ok=true`, `state_db_fallback_in_use=false`
- `/readyz` → `ready=true`
- `/api/projects/sandbox_project/history` → `count=3`
- `/api/projects/sandbox_project/threads` → `count=3`
- latest persisted task still remains `task-1779122095-e24170`

Current roster impact after this blocked live attempt:

- live identities remain exactly `3`
- `coordinator_agent` remains live
- `writer_agent` remains live
- `reviewer_agent` remains live
- `security_agent` is still allowed by the runtime contract but not yet live on
  this machine

## What remains intentionally not done

- `devops_agent` is still not activated
- `data_agent` is still not activated
- all specialist roles are still not live
- 20–30 live agents are still not running
- Hedgekeeper is still not attached
- write-assisted main-project work is still not enabled
- VPS/prod rollout is still separate

## Outcome

Outcome: `security_agent live identity partially blocked`

The blocker is exact and bounded:

- the runtime contract is already correct
- the tests are already correct
- the docs are already correct
- the only missing part for real activation is the absent local
  `TELEGRAM_SECURITY_BOT_TOKEN`

## Handoff to the next local step

To move this exact step from blocked-path to live-proof:

1. add a real local `TELEGRAM_SECURITY_BOT_TOKEN`
2. extend the real local `TELEGRAM_AGENT_TOKENS` with
   `security_agent=TELEGRAM_SECURITY_BOT_TOKEN`
3. restart the live local Telegram runtime
4. verify:
   - Telegram `getMe`
   - startup
   - polling
   - one bounded direct DM proof against the live `security_agent` identity
