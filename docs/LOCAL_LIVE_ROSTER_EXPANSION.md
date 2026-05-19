# Local Live Roster Expansion

Outcome: `live roster expansion partially blocked`

## Step scope

This step attempts the next bounded live Telegram roster wave after the
current three-identity contour already proved:

- real Telegram transport
- role-aware bounded task loops
- owner-facing diagnostics and recovery
- one successful live Telegram free-text task

It does **not**:

- attach Hedgekeeper
- enable write-assisted main-project work
- start VPS or production rollout
- jump straight to 20-30 live Telegram identities
- pretend that logical pipeline roles are already separate live bot
  identities

It does:

- inspect the exact current Telegram token preconditions
- verify whether new baseline-wave identities can actually be mapped
- keep the live-vs-logical boundary explicit
- record a truthful per-role delivery blocker when new identities are still
  missing

## Exact current roster before expansion

Before attempting any new wave, the real live roster remained exactly the same
three Telegram identities already certified in `L0.5-L0.9`:

- `coordinator_agent` → `@ai_dev_team_lead_bot`
- `writer_agent` → `@ai_dev_team_writer_bot`
- `reviewer_agent` → `@ai_dev_team_reviewer_bot`

This remained true both in docs and in actual env/runtime assembly:

- `TELEGRAM_AGENT_TOKENS`
  - `coordinator_agent=TELEGRAM_BOT_TOKEN`
  - `reviewer_agent=TELEGRAM_REVIEWER_BOT_TOKEN`
  - `writer_agent=TELEGRAM_WRITER_BOT_TOKEN`

Current live local contour facts remained:

- `STATE_DB_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/state/state.db`
- `OBS_LOG_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/logs/pipeline-log.jsonl`
- `project_id=sandbox_project`
- `REPO_PATH=/Users/efimenko_k/sandbox-project`

## Exact token / startup preconditions

The current `.env` still exposes only these relevant Telegram runtime keys:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_OWNER_CHAT_ID`
- `TELEGRAM_WRITER_BOT_TOKEN`
- `TELEGRAM_REVIEWER_BOT_TOKEN`
- `TELEGRAM_AGENT_TOKENS`

That means the startup preconditions for the **existing** three-bot contour are
present, but the startup preconditions for the next baseline-wave identities
are not present in `.env`.

For the preferred next baseline-wave roles:

- `planning_agent`
- `pm_agent`
- `architect_agent`
- `tester_agent`
- `qa_agent`
- `fixer_agent`

there are currently **no** additional role bindings in `TELEGRAM_AGENT_TOKENS`,
and there are **no** corresponding extra Telegram bot token keys available in
the checked `.env`.

## Exact new live identity set or blocker

No new live identities could be truthfully added on this step.

The exact blocker is configuration-level and bounded:

- `build_multi_bot_runtime_spec_from_env(...)` still resolves only:
  - `coordinator_agent`
  - `reviewer_agent`
  - `writer_agent`
- there is no fourth live role binding to even probe via `getMe`
- therefore there is no truthful startup path for:
  - `planning_agent`
  - `pm_agent`
  - `architect_agent`
  - `tester_agent`
  - `qa_agent`
  - `fixer_agent`

So the attempted next baseline-wave expansion blocks **before** live PTB
startup for any new role.

## Per-role delivery proof

There is no truthful per-role delivery proof for any **new** live identity on
this step, because no new live identity exists yet.

What still remains proven from earlier local steps:

- `coordinator_agent`
  - real DM `/help` proof through `@ai_dev_team_lead_bot`
- `writer_agent`
  - real identity startup + reachability proof on the Mac
- `reviewer_agent`
  - real identity startup + reachability proof on the Mac

What is **not** proven yet:

- direct DM delivery to `planning_agent`
- direct DM delivery to `pm_agent`
- direct DM delivery to `architect_agent`
- direct DM delivery to `tester_agent`
- direct DM delivery to `qa_agent`
- direct DM delivery to `fixer_agent`

This is not a transport or routing bug. It is simply the absence of additional
real Telegram bot identities in the current env.

## Live vs logical boundary after attempted expansion

This boundary is the key truthfulness result of `L0.10`.

### Real live Telegram identities now

Still only:

- `coordinator_agent`
- `writer_agent`
- `reviewer_agent`

### Runtime-exposed roles in the product catalog

The product catalog in `core/agent_role_catalog.py` now classifies the whole
baseline internal team plus the later promoted specialist subset as
runtime-exposed roles:

- `coordinator_agent`
- `planning_agent`
- `pm_agent`
- `architect_agent`
- `writer_agent`
- `reviewer_agent`
- `tester_agent`
- `qa_agent`
- `fixer_agent`
- `security_agent`
- `devops_agent`

Important truth boundary after the later specialist promotion steps:

- `security_agent` is runtime-exposed in the catalog
- `security_agent` still remains a specialist role, not a baseline internal
  team member
- `security_agent` becomes live only when `TELEGRAM_AGENT_TOKENS` explicitly
  maps it to `TELEGRAM_SECURITY_BOT_TOKEN`
- `devops_agent` is also runtime-exposed in the catalog after the later second
  specialist promotion contract
- `devops_agent` still remains a specialist role, not a baseline internal
  team member
- `devops_agent` becomes live only when `TELEGRAM_AGENT_TOKENS` explicitly
  maps it to `TELEGRAM_DEVOPS_BOT_TOKEN`
- this `L0.10` artifact still truthfully records that the actual live roster
  at that step stayed at only three separate Telegram identities

### Logical pipeline roles already proven in execution

The live task loops already proved broader logical participation for:

- `planning_agent`
- `pm_agent`
- `architect_agent`
- `writer_agent`
- `reviewer_agent`
- `tester_agent`
- `qa_agent`
- `fixer_agent`

### Truthful boundary

So after this step the correct statement is:

- more roles exist logically and can run in the pipeline
- more roles are cataloged as runtime-exposed candidates
- `security_agent` is now an allowed specialist live-identity candidate, but
  it was not yet started live during `L0.10`
- but only three roles are actually separate live Telegram identities today

This step does **not** allow anyone to say that the whole baseline team is
already live as separate bots.

## Operator-visible aftermath

Because no new live identities were configured, this step did **not** create
any new task/thread state or fake delivery artifacts.

The persisted live SQLite state remained unchanged from `L0.9`:

- `project_task_count=3`
- `thread_count=3`
- latest task still `task-1779122095-e24170`

Web Office/API truth remained healthy on the same live DB:

- `/healthz`
  - `ok=true`
  - `state_db_fallback_in_use=false`
- `/readyz`
  - `ready=true`
- `/api/projects/sandbox_project/history`
  - `count=3`
  - latest item still `task-1779122095-e24170`

This is the correct operator-facing aftermath for a blocked expansion step:

- the current live AI Office remains valid
- no fake extra agents appear
- no fake extra threads appear
- no fake roster jump is implied anywhere

## What is still intentionally not done

This step still does **not**:

- claim any fourth live Telegram identity
- claim a successful per-role DM proof for planning/pm/architect/tester/qa/fixer
- attach Hedgekeeper
- enable write-assisted main-project work
- start VPS or production deployment work
- claim that 20-30 live agents are already running

## Handoff to the next local step

The next bounded roster step should happen only after additional real Telegram
bot tokens exist and are mapped.

Truthful next actions:

1. provision one or more new Telegram bot identities
2. add their env keys and extend `TELEGRAM_AGENT_TOKENS`
3. restart the local multi-bot runtime
4. prove reachability and one bounded per-role DM round-trip for the newly
   added role(s)
