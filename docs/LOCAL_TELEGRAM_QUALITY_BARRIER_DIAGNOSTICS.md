# Local Telegram Quality-Barrier Diagnostics

Outcome: `owner-visible quality-barrier diagnostics partially blocked`

## Step scope

This step improves the owner-visible diagnostics layer for bounded live
Telegram task failures that currently end with
`review_fix_loop_exceeded`.

It does **not**:

- attach Hedgekeeper
- enable write-assisted main-project work
- provision a wider live Telegram roster
- chase the first live `SUCCESS` at any cost
- start VPS or production rollout
- claim that the local AI Office is already fully transparent in every
  failure path

It does:

- preserve the existing real live Telegram contour
- identify the exact diagnostics gap in the current owner-facing failure path
- reuse existing truthful review / QA artifacts instead of inventing a new
  subsystem
- apply a bounded diagnostics bridge for Telegram `/log` and Web Office
  history surfaces
- document the exact blocker that still prevents full live certification

## Exact current live contour facts

The bounded live contour stayed on the same three real Telegram identities:

- `coordinator_agent` → `@ai_dev_team_lead_bot`
- `writer_agent` → `@ai_dev_team_writer_bot`
- `reviewer_agent` → `@ai_dev_team_reviewer_bot`

Shared live runtime paths for the certified contour:

- `STATE_DB_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/state/state.db`
- `OBS_LOG_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/logs/pipeline-log.jsonl`

Live sandbox target facts remain:

- `project_id=sandbox_project`
- `REPO_PATH=/Users/efimenko_k/sandbox-project`

Two real live Telegram task loops are still the active failure reference for
this step:

- `task-1779096156-c9f73a`
- `task-1779102006-e4d531`

Persisted truth in the same live SQLite DB still says:

- `project_task_count=2`
- `thread_count=2`
- both latest tasks ended with `FAIL`
- both latest tasks persisted `failure_reason=review_fix_loop_exceeded`

## Exact current diagnostics gap

Before this step, the owner-facing recovery path was truthful but too coarse.

The owner could already see:

- `task_id`
- `FAIL`
- a generic coordinator summary about the quality barrier
- the bounded code `review_fix_loop_exceeded`

But the owner could **not** see an actionable short explanation of what the
review or QA barrier actually wanted next.

Current coarse live examples on the same persisted task
`task-1779102006-e4d531`:

- Telegram final reply ended with `reason  review_fix_loop_exceeded`
- `/log task-1779102006-e4d531` currently renders only:
  - `⚠️ Причина: review_fix_loop_exceeded`
- `sqlite3 ...state.db` still returns only:
  - `task-1779102006-e4d531|FAIL|review_fix_loop_exceeded`
- `/api/projects/sandbox_project/history` on the same live DB currently returns:
  - `failure_reason=review_fix_loop_exceeded`
  - `failure_detail=null`
- `/projects/sandbox_project/history` on the same live DB still has
  `Failure reason: review_fix_loop_exceeded` and no `Failure detail:` block

So the exact gap was not transport, routing, or absence of live bots. The gap
was that actionable review / fix detail existed during the pipeline, but it
did not reach the owner as a compact follow-up hint.

## Truth source for the diagnostics

The truthful diagnostics source already exists inside the pipeline:

- `PipelineMemory` `review` artifact for review-barrier failures
- `PipelineMemory` `qa` artifact for QA-barrier failures
- `for_fixer` instructions inside those artifacts
- verdict / severity summary already produced by reviewer or QA
- the same persisted `TaskSummary` record that later feeds `/log` and Web Office

The observability log is also useful, but only as a coarse trace:

- it proves which logical roles ran
- it proves the task really reached reviewer / fixer / tester / qa stages
- it does **not** by itself give the owner a compact next-fix instruction

This step therefore used a bounded bridge, not a new subsystem:

- `core/real_task_handler.py`
  - builds a short diagnostics preview from the existing `review` or `qa`
    artifact
- `core/task_history.py`
  - persists `failure_reason` and `failure_detail` together in one stable
    string format
- `core/bot_runner.py`
  - splits that persisted format back into:
    - `⚠️ Причина: <reason_code>`
    - `🧭 Диагностика: <detail>`
- `web/main.py`
  - serializes separate `failure_reason` and `failure_detail` fields
- `web/templates/project.html`
- `web/templates/project_history.html`
- `web/static/dashboard.css`
  - surface the same bounded detail without changing core route semantics

## Bounded fix applied

The bounded fix did **not** change task orchestration semantics.

It only changed how already-existing failure truth is surfaced:

1. review-barrier failures now produce a compact preview such as:
   - `review=REJECTED; summary c=0 m=1 n=0; next fix: major src/example.py: restore square implementation`
2. QA-barrier failures now have an equivalent preview path when a `qa`
   artifact exists
3. `/log <task_id>` can now show the diagnostics as a separate owner-facing
   line
4. Web Office history / recent-task surfaces can now show:
   - `failure_reason`
   - `failure_detail`

The new diagnostics preview is intentionally short, bounded, and derived from
the pipeline's own structured outputs.

## Owner-visible aftermath

Current live task records from before this patch remain coarse. That is still
the exact truth.

What is now proven by bounded code-level diagnostics tests:

- a new review-barrier failure can persist:
  - `failure_reason=review_fix_loop_exceeded`
  - `failure_detail=review=REJECTED; summary c=0 m=1 n=0; next fix: major src/example.py: restore square implementation`
- the final Telegram failure message can now include both:
  - `detail  review=REJECTED; summary c=0 m=1 n=0; next fix: major src/example.py: restore square implementation`
  - `reason  review_fix_loop_exceeded`
- `/log task-quality-fail` can now show:
  - `⚠️ Причина: review_fix_loop_exceeded`
  - `🧭 Диагностика: review=REJECTED; next fix: src/example.py: restore square implementation`

This is enough to make the next owner decision more informed:

- refine the task prompt
- approve or reject the requested fix direction
- decide whether to retry

But it is **not** yet a fresh live Telegram proof on the already-running
contour.

## Web Office / log aftermath

Web Office truth was rechecked directly through `web.main` against the same
live `STATE_DB_PATH`, because the temporary localhost server on `127.0.0.1:8004`
was not running during this step.

Current live DB readback still shows the pre-patch tasks as:

- `failure_reason=review_fix_loop_exceeded`
- `failure_detail=null`

This means:

- `/api/projects/sandbox_project/history` is still truthful
- `/projects/sandbox_project/history` is still truthful
- `/log task-1779102006-e4d531` is still truthful
- but none of those old live artifacts can retroactively become more
  explanatory without inventing data

What is now proven for future persisted failures:

- `/api/projects/<project_id>/history` returns split fields
- `/projects/<project_id>/history` renders `Failure detail:` when available
- `/projects/<project_id>` recent-task preview renders the same short detail

Health truth remained intact during the same readback:

- `/healthz` returned `ok=true`
- `state_db_path=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/state/state.db`
- `state_db_fallback_in_use=false`

## Exact blocker

The blocker that prevents full live certification on this step is narrow and
explicit:

- the two real live failure records in the current SQLite DB were created
  before the diagnostics bridge existed
- no fresh live Telegram failure was re-run after the patch
- therefore the current live owner chat and persisted live history do not yet
  contain the new `failure_detail` field

This is why the step is recorded as partially blocked instead of falsely
claiming that the live owner already sees the richer diagnostics end-to-end.

## What is still intentionally not done

This step still does **not**:

- certify the first successful live Telegram task
- attach Hedgekeeper
- enable write-assisted main-project work
- provision 20–30 live Telegram identities
- start VPS or production deployment work
- backfill old live failures with invented diagnostics

## Handoff to the next local step

The next bounded local follow-up should be:

1. restart or reuse the updated local Telegram runtime
2. run one new bounded sandbox task that is allowed to fail honestly if the
   quality barrier still triggers
3. verify that the owner now sees both:
   - raw reason code
   - actionable diagnostics detail
4. only after that chase the first successful live Telegram task again
