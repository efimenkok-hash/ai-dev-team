# Local Live Diagnostics Detail Certification

Outcome: `live diagnostics detail partially blocked`

## Step scope

This step runs one fresh bounded live Telegram task loop on the patched
runtime from `L0.8`.

It does **not**:

- attach Hedgekeeper
- enable write-assisted main-project work
- provision a wider live Telegram roster
- start VPS or production rollout
- claim a fake live failure-detail proof when the fresh run does not actually
  fail

It does:

- reuse the same real live local Telegram contour
- reuse the same safe sandbox repo and isolated live SQLite state
- run one fresh owner-sent bounded Telegram task on the patched runtime
- verify the Telegram outcome, `/log`, persisted state, and Web Office/API
  aftermath
- record the exact blocker if the new live run does not exercise the
  failure-detail path

## Exact current live contour facts

The fresh `L0.9` run reused the same bounded three-identity live contour:

- `coordinator_agent` → `@ai_dev_team_lead_bot`
- `writer_agent` → `@ai_dev_team_writer_bot`
- `reviewer_agent` → `@ai_dev_team_reviewer_bot`

The runtime was restarted on the same isolated live paths:

- `STATE_DB_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/state/state.db`
- `OBS_LOG_PATH=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/logs/pipeline-log.jsonl`

The same safe sandbox target remained the only write target:

- `project_id=sandbox_project`
- `REPO_PATH=/Users/efimenko_k/sandbox-project`

Fresh runtime startup on the patched code was re-verified before the run:

- `coordinator_agent` reachable as `@ai_dev_team_lead_bot`
- `reviewer_agent` reachable as `@ai_dev_team_reviewer_bot`
- `writer_agent` reachable as `@ai_dev_team_writer_bot`
- all three PTB applications reached `started=True`
- all three reached `polling_started=True`

## Exact fresh task statement

The fresh owner message sent through the live Telegram contour was:

> `В sandbox-project добавь функцию square(x: int) -> int в src/example.py и один pytest-тест test_square() в tests/test_example.py с assert square(3) == 9. README.md не меняй. Ничего кроме этих двух файлов не трогай.`

The owner also explicitly set:

- `/tier set ECONOMY`

Why this task was chosen:

- it is the same small deterministic sandbox task used in the previous
  bounded live proof
- it keeps the review surface narrow
- it is honest work, not a prompt artificially engineered to force failure
- it can either fail with actionable diagnostics or succeed cleanly on the
  patched runtime

## Actual runtime path used

The actual fresh live path stayed on the real Telegram contour:

1. real owner DM to `@ai_dev_team_lead_bot`
2. `TelegramBridge` accepted the free-text task
3. `ProjectRuntimeRouter` resolved `sandbox_project`
4. the bounded task ran against `/Users/efimenko_k/sandbox-project`
5. the logical pipeline invoked:
   - `planning_agent`
   - `pm_agent`
   - `architect_agent`
   - `writer_agent`
   - `reviewer_agent`
   - `tester_agent`
   - `qa_agent`
   - `fixer_agent`
   - `reviewer_agent`
   - `tester_agent`
   - `qa_agent`
6. `TaskHistory` persisted the final result into the same live SQLite DB
7. `/log` readback used the same persisted record
8. Web Office/API readback used the same `STATE_DB_PATH`

This stayed a real live transport proof:

- no synthetic bridge simulation
- no mock-only path
- no backfill of old failure records

## Actual live outcome

The fresh live run unexpectedly reached `SUCCESS`.

Exact persisted result:

- `task_id=task-1779122095-e24170`
- `tier=ECONOMY`
- `final_state=SUCCESS`
- `branch=feature/task-1779122095-e24170`
- `commit_sha=d2e9d5ac8e65eb2d2ee200216cdec2e9268ed1d5`
- `failure_reason=null`
- `failure_detail=null`

Bounded repo artifact truth:

- `git show --stat --oneline d2e9d5ac` returned:
  - `src/example.py`
  - `tests/test_example.py`
- the commit stayed bounded to the expected two-file task surface

Non-fatal additional operator-facing warning still surfaced:

- `🧩 Логический hire не удалось обработать; persisted project roster не менялся.`
- technical reason: `ValueError: unknown_specialist_role:writer_agent`

That warning did **not** change the persisted roster and did **not** block the
task from reaching `SUCCESS`.

## Owner-facing Telegram output

Fresh owner-visible Telegram evidence from the real chat:

> `🚀 Старт · task_id=task-1779122095-e24170 · тариф ECONOMY`
>
> `Координатор: 🚀 Принял в работу`
>
> `task-id task-1779122095-e24170`
>
> `тариф ECONOMY`

The owner then saw the real live progress stream:

- `planning_agent`
- `pm_agent`
- `architect_agent`
- `writer_agent`
- `reviewer_agent`
- `tester_agent`
- `qa_agent`
- `fixer_agent`
- `reviewer_agent`
- `tester_agent`
- `qa_agent`

Final owner-facing live Telegram result:

> `🏁 Готово · branch=feature/task-1779122095-e24170 · commit=d2e9d5ac`
>
> `✅ Готово`
>
> `task-id task-1779122095-e24170`
>
> `тариф ECONOMY`
>
> `branch feature/task-1779122095-e24170`
>
> `commit d2e9d5ac`

This is truthful and useful for the owner, but it is a success path. It does
**not** exercise the new owner-visible failure-detail layer.

## `/log` aftermath

`/log task-1779122095-e24170` on the same live DB now returns a truthful
success summary:

- `✅ Статус: SUCCESS`
- `🌿 Ветка: feature/task-1779122095-e24170`
- `🔖 SHA: d2e9d5a`
- `💼 Тариф: ECONOMY`

Important truth boundary:

- `/log` does **not** show `⚠️ Причина`
- `/log` does **not** show `🧭 Диагностика`
- that is correct because the fresh run did not fail

So `/log` itself is healthy and truthful, but this run still did not prove the
live failure-detail branch end-to-end.

## Web Office / API aftermath

Web Office and API readback were re-checked against the same live
`STATE_DB_PATH` through `web.main`.

Health truth stayed intact:

- `/healthz`
  - `ok=true`
  - `state_db_path=/private/tmp/ai-dev-team-live-telegram-l05/20260518T085123Z/state/state.db`
  - `state_db_fallback_in_use=false`
- `/readyz`
  - `ok=true`
  - `ready=true`
  - same live `state_db_path`

Fresh persisted history truth:

- `project_task_count=3`
- `thread_count=3`
- latest task:
  - `task_id=task-1779122095-e24170`
  - `final_state=SUCCESS`
  - `branch=feature/task-1779122095-e24170`
  - `commit_sha=d2e9d5ac8e65eb2d2ee200216cdec2e9268ed1d5`
  - `failure_reason=null`
  - `failure_detail=null`
- latest thread:
  - `thread_id=thread_000003`
  - `opened_by_role=coordinator_agent`
  - `task_id=task-1779122095-e24170`

Web Office/API operator-facing aftermath:

- `/projects/sandbox_project`
  - shows `task-1779122095-e24170`
  - shows `SUCCESS`
  - includes the new recent task in the project preview
- `/projects/sandbox_project/history`
  - shows `task-1779122095-e24170`
  - shows `SUCCESS`
  - does **not** show `Failure detail:` for the latest task
- `/api/projects/sandbox_project/history`
  - returns `count=3`
  - latest item has:
    - `failure_reason=null`
    - `failure_detail=null`
- `/api/projects/sandbox_project/threads`
  - returns `count=3`
  - latest thread is `thread_000003`
- `/projects/sandbox_project/team`
  - still shows baseline internal team `9`
  - still shows `Approved specialists = 0`
  - still shows `Pending hire requests = 0`

This is truthful end-to-end for the fresh success run, but it still does not
exercise the new failure-detail path on a live persisted failure record.

## Exact blocker

The blocker is now narrower than in `L0.8`, but it still exists:

- the patched runtime was successfully re-run live
- the fresh owner-sent bounded task did complete live
- however the fresh run reached `SUCCESS`, so no new live failure record was
  created
- therefore the new owner-visible `failure_detail` path still was not
  exercised on a fresh real Telegram failure

This means:

- live transport proof is real
- patched runtime proof is real
- persisted/Web Office/API readback is real
- live failure-detail certification is still not fully exercised

## What is still intentionally not done

This step still does **not**:

- fake a failure to force the diagnostics bridge
- claim that the failure-detail path is already live-certified end-to-end
- attach Hedgekeeper
- enable write-assisted main-project work
- expand the live roster beyond the current three identities
- start VPS or production deployment work

## Handoff to the next local step

The next bounded local step should intentionally stay on the current live
contour and do one of two things:

1. run another small honest sandbox task that may naturally fail, then verify
   the new `reason + detail` path live
2. if the product direction shifts, accept that the runtime is getting
   healthier and pivot to the next highest-value local AI Office capability
