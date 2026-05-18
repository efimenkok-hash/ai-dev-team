# L0.14 — direct-DM role voice for live specialist bots

## Step scope

This step fixes only the direct personal bot voice gap that remained after the
accepted live activation of `security_agent`:

- direct DM to a specific live bot identity must answer in that bot's own role
  voice/signature
- this includes slash-command replies handled in that bot's direct DM context
- coordinator remains the orchestrator and control-plane voice where
  coordinator semantics are actually intended

This step does **not**:

- activate any new bot
- expand the specialist wave beyond `security_agent`
- change multi-bot runtime expansion logic
- attach Hedgekeeper
- enable write-assisted main-project work
- start VPS/prod rollout
- introduce any broad UI or orchestration refactor

## Exact old behavior

The accepted `L0.13` live proof had already established that:

- direct DM to `@ai_dev_team_security_agent_bot` really worked
- the live runtime really delivered the reply through that live identity path

But the owner-visible reply was still:

> `Координатор: 🛠 Доступные команды`

The old behavior came from the command path in
`core/telegram_bridge.py`:

- slash-command replies were always signed through `_sign_coordinator(...)`
- `_safe_send(...)` used the default `sender_role=COORDINATOR_ROLE`
- the incoming secondary direct-DM bot context was preserved only as
  `delivery_role`, not as the visible reply voice

So the old direct-DM experience was truthful transport-wise, but wrong as a
personal bot UX contract.

## Exact new direct-DM role voice contract

The new narrow contract for this step is:

- direct DM to a specific live bot identity answers in that bot's own role
  voice/signature
- this applies to slash-command replies when the inbound message arrived in
  owner DM via a known live `incoming_bot_role`
- coordinator still speaks as coordinator for:
  - coordinator-owned direct DM
  - general command flows without a specific live bot context
  - safe fallback paths when the inbound role is unknown

In other words:

- personal specialist DM should feel personal
- control-plane semantics stay with the coordinator

## Exact code path changed

The fix stayed narrow and local to the bridge command path:

- [core/telegram_bridge.py](/Users/efimenko_k/ai-dev-team/core/telegram_bridge.py)
  now resolves a command reply role through
  `_resolve_command_reply_role(msg)`
- `_handle_command(...)` now:
  - signs slash-command replies with `_sign_with_role(reply_role, ...)`
  - sends them with `sender_role=reply_role`
  - keeps `incoming` so delivery-role routing still works

What did **not** change:

- free-text task reply signing path
- coordinator fallback behavior for unknown roles
- delivery-role routing for secondary owner DM threads
- multi-bot runtime activation logic

## Live proof

Fresh live proof on the patched runtime:

- direct target chat: `@ai_dev_team_security_agent_bot`
- exact inbound message: `/help`
- old observed reply prefix before this fix:
  `Координатор: 🛠 Доступные команды`
- new observed reply prefix after this fix:
  `Безопасник: 🛠 Доступные команды`

This truthfully proves:

- the message still reached the same live `security_agent` identity
- the owner-visible slash-command reply no longer defaults to
  `Координатор:` in that direct context
- the direct personal bot experience now matches the bot identity that the
  owner actually addressed

## Non-regression aftermath

The patched live runtime was restarted on the same isolated live DB/log path:

- `STATE_DB_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/state/state.db`
- `OBS_LOG_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/logs/pipeline-log.jsonl`

Patched 4-bot runtime startup remained healthy:

- `coordinator_agent` reachable as `@ai_dev_team_lead_bot`
- `reviewer_agent` reachable as `@ai_dev_team_reviewer_bot`
- `security_agent` reachable as `@ai_dev_team_security_agent_bot`
- `writer_agent` reachable as `@ai_dev_team_writer_bot`
- all four roles reached `token_valid=true`, `reachable=true`,
  `started=true`, `polling_started=true`

Operator-surface readback against the same live `STATE_DB_PATH` stayed
truthful:

- `/healthz` → `ok=true`, `state_db_fallback_in_use=false`
- `/readyz` → `ready=true`, `state_db_fallback_in_use=false`
- `/api/projects/sandbox_project/history` → `count=3`
- `/api/projects/sandbox_project/threads` → `count=3`
- latest persisted task still remains `task-1779122095-e24170`

Important truthfulness note:

- the localhost HTTP server on `127.0.0.1:8004` was not running during this
  step
- these surfaces were therefore verified via `web.main` + `TestClient`
  against the same live DB, not through an external HTTP process
- the `/help` proof did not create fake project tasks or threads

## Outcome

Outcome: `direct DM role voice certified`

## What remains intentionally not done

- no new specialist bot was activated beyond `security_agent`
- `devops_agent` is still not live
- `data_agent` is still not live
- 20–30 live agents are still future work
- Hedgekeeper is still not attached
- write-assisted main-project work is still not enabled
- VPS/prod rollout is still separate

## Handoff to next step

After `L0.14`, the remaining gap is no longer personal specialist voice for
the first promoted bot. The next correct step is separate from this fix:

- either promote the next specialist live identity
- or expand personal-bot UX intentionally for more direct roles
- but not by reopening the already-closed `security_agent` direct-DM voice gap
