# Local Pilot Task Execution

Outcome: `pilot task executed locally`

## Local pilot task scope

This step proves one real end-to-end AI Dev Team task inside the isolated
local contour from `L0.1-L0.3`.

It does **not**:

- use Docker
- attach to the live Docker-mounted main project
- use assist-mode against the real main project
- depend on VPS rollout, systemd, nginx, domain, or HTTPS

It does:

- use a disposable sandbox git repo
- use the existing local control-plane seams
- run one real OpenRouter-backed task
- verify persisted project/thread/history aftermath through Web Office

## Exact sandbox target facts

Canonical execution root for this step:

- `/private/tmp/ai-dev-team-local-pilot-l04/20260518T074824Z`

Exact isolated target paths:

- sandbox repo:
  `/private/tmp/ai-dev-team-local-pilot-l04/20260518T074824Z/sandbox_repo`
- persisted state DB:
  `/private/tmp/ai-dev-team-local-pilot-l04/20260518T074824Z/state/state.db`
- worktree root:
  `/private/tmp/ai-dev-team-local-pilot-l04/20260518T074824Z/worktrees`

Repo type:

- disposable local git repo created specifically for this step
- initial branch: `main`
- initial commit:
  `b310e4e9ed8749c4a3ace4c1ef315da6fd5c7208`

Why this target is safe:

- it was created only for `L0.4`
- it lives under `/private/tmp`, outside the live main project
- it does not reuse the Docker-mounted main working tree
- it can be deleted without affecting any real product repository

## Exact task statement

Task sent into the local contour:

> В sandbox repo добавь модуль `src/example.py` с функцией `square(x: int) -> int`, которая возвращает `x * x`. Добавь тест `tests/test_example.py` с проверкой `square(3) == 9` и коротко обнови `README.md`, чтобы описать новую функцию и как запустить тест. Не трогай ничего вне этого репозитория.

## Expected result before run

Before execution, the expected result was:

- one persisted project-aware task run against the disposable sandbox repo
- one new branch under the sandbox repo worktree flow
- one committed result if the pipeline reaches `SUCCESS`
- exactly these functional artifacts in the target repo:
  - `src/example.py`
  - `tests/test_example.py`
  - updated `README.md`
- operator-visible aftermath in Web Office:
  - project overview shows one recent task
  - history page shows one persisted task record
  - team page remains truthful and does not invent runtime activations

## Execution path actually used

The execution did **not** use live Telegram transport, even though the product
still has Telegram-facing runtime surfaces.

The path actually used was the existing transport-agnostic local control path:

1. Synthetic isolated owner-DM input was passed into `TelegramBridge`.
2. `/tier set ECONOMY` was executed through the existing slash-command path.
3. The free-text task was accepted by the same `TelegramBridge`.
4. `make_real_task_handler(...)` ran the real pipeline asynchronously.
5. `build_dispatcher_from_env(...)` supplied the real OpenRouter-backed
   dispatcher path.
6. `ProjectRuntimeRouter` resolved the sandbox runtime for
   `project_id=pilot_sandbox`.
7. `TaskHistory(state_db=...)` mirrored the completed task into SQLite.
8. `web.main` read the persisted aftermath back through dashboard/project/team/
   history/health surfaces.

Local operator mode for this step:

- no Docker
- no live Telegram message delivery
- no live main-project attach
- no VPS/server rollout

## Actual result

The canonical successful run for this step is:

- task id: `task-1779090504-4cd765`
- tier: `ECONOMY`
- final state: `SUCCESS`
- branch: `feature/task-1779090504-4cd765`
- commit:
  `be223a03f65b7153833d2dde2295f9a75d3f40e3`

Observed agent progress in the real run:

- `planning_agent`
- `pm_agent`
- `architect_agent`
- `writer_agent`
- `reviewer_agent`
- `tester_agent`
- `qa_agent`

Immediate control-plane evidence:

- `/tier set ECONOMY` returned a normal coordinator confirmation
- the task was accepted with a real `task-id`
- progress messages showed real agent transitions
- the final terminal message was:
  `✅ Готово ... branch feature/task-1779090504-4cd765 ... commit be223a03`

## Operator-visible evidence

Persisted project/thread/task facts after execution:

- project: `pilot_sandbox`
- thread count: `1`
- persisted thread id: `thread_000001`
- thread opener: `coordinator_agent`
- persisted task history count for the project: `1`

State-backed task summary recorded in SQLite:

- `task_id=task-1779090504-4cd765`
- `final_state=SUCCESS`
- `branch=feature/task-1779090504-4cd765`
- `commit_sha=be223a03f65b7153833d2dde2295f9a75d3f40e3`
- `project_id=pilot_sandbox`

## Web Office aftermath

The persisted aftermath was verified both through local app reads and through
an isolated localhost Web Office session on `127.0.0.1:8004`.

Dashboard (`/`):

- rendered `Pilot Sandbox Project`
- showed `1` total project
- showed the project as `active`
- linked to `/projects/pilot_sandbox`

Project overview (`/projects/pilot_sandbox`):

- showed `Resolved team size = 9`
- showed `Recent tasks = 1`
- showed `Persisted threads = 1`
- showed the exact task id `task-1779090504-4cd765`
- showed the exact thread id `thread_000001`

History page (`/projects/pilot_sandbox/history`):

- rendered `Recent persisted tasks = 1`
- showed `task-1779090504-4cd765`
- showed `SUCCESS`
- showed branch `feature/task-1779090504-4cd765`
- showed commit SHA
  `be223a03f65b7153833d2dde2295f9a75d3f40e3`

Team page (`/projects/pilot_sandbox/team`):

- remained truthful about the baseline internal team of `9`
- showed `0` approved specialists
- showed `0` pending hire requests
- did not invent runtime activation of specialists

Health surfaces:

- `/healthz` returned `ok=true`
- `/readyz` returned `ready=true`

## Produced repo / artifact result

The committed sandbox diff created exactly three files of interest:

- `README.md`
- `src/example.py`
- `tests/test_example.py`

Committed `src/example.py`:

```python
def square(x: int) -> int:
    return x * x
```

Committed test file:

```python
import unittest
from src.example import square


class TestSquare(unittest.TestCase):
    def test_positive_number(self):
        self.assertEqual(square(3), 9)

    def test_negative_number(self):
        self.assertEqual(square(-4), 16)

    def test_zero(self):
        self.assertEqual(square(0), 0)
```

Committed README summary:

- describes the `square` function
- documents how to run the test locally

Post-run verification on the committed sandbox result:

- `ruff check .` — passed
- `pytest -q` — `3 passed`

## Bounded observations

One bounded non-fatal observation surfaced during the successful run:

- logical-hiring follow-up emitted:
  `ValueError: unknown_specialist_role:writer_agent`

What this means in practice:

- the sandbox task itself still completed successfully
- persisted project roster did not change
- team view remained truthful with `0` approved specialists and `0` pending
  hire requests

This is a residual product observation for future tightening, not a blocker
for `L0.4`.

## What this step still does not prove

This step still does **not** prove:

- safe attach to the live main project
- assist-mode against the real main project
- VPS/server deployment
- production serving
- domain / HTTPS
- server-side backup automation

## Handoff to L0.5

`L0.4` now has one real successful sandbox-only task execution inside the
isolated local contour.

The next step can therefore focus on:

- `L0.5 — safe attach to the main project in assist-mode`

The handoff is explicit:

- local control path is real
- local Web Office aftermath is real
- persisted history/thread evidence is real
- the remaining boundary is safe attachment to the real main project, not
  another synthetic sandbox proof
