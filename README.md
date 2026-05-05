# AI Dev Team

Multi-agent engineering platform with strict FSM, repo awareness, and contract enforcement. Implementation of the `AI_Dev_Team_v4_ULTRA` specification.

The platform takes a single natural-language task and drives it through a fixed pipeline of eight specialised agents (planning → PM → architect → writer → reviewer → tester → QA → fixer) with hard-coded transition rules, retry limits, and FAIL_SAFE behaviour. It is designed to be plugged into multiple external Python projects in parallel via project adapters.

## Status

| Item | Value |
|---|---|
| Tests | 411 passed, 0 failed |
| Coverage | 93.1% |
| Ruff | 0 violations |
| Python | >= 3.10 |

## Repository layout

```
core/
├── adapter.py            project adapter & registry (multi-project support)
├── agents.py             8 agent prompt builders
├── call_graph.py         AST-based call graph
├── code_retriever.py     semantic search over project files
├── contracts.py          FSM invariants, protected files, forbidden tokens
├── dependency_graph.py   AST-based dependency graph
├── fsm.py                states, transitions, retry/loop limits
├── git_integration.py    safe git CLI wrapper (branch, commit, PR draft, rollback)
├── impact_analysis.py    transitive reverse-dependency impact
├── memory.py             pipeline memory store, dump/restore
├── observability.py      structured logs, metrics, agent perf, cost tracking
├── orchestrator.py       FSM driver with task validators + cost budget
├── patcher.py            atomic diff/preview/apply
├── quality_gates.py      programmatic ruff + pytest + coverage runner
├── repo_reader.py        text-file enumeration
├── router.py             Ollama / OpenRouter routing
└── vector_store.py       FAISS L2 wrapper

tests/                    411 unit tests
tests/integration/        opt-in real-LLM smoke tests (require AI_DEV_TEAM_REAL_LLM=1)
docs/fsm_spec.md          state machine specification
scripts/quality_check.sh  local quality gate entry point
main.py                   CLI entry point
```

## Quick start

```bash
git clone <your-fork-url> ai-dev-team
cd ai-dev-team

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
echo "OPENROUTER_API_KEY=sk-or-v1-..." >> .env

bash scripts/quality_check.sh
python main.py "Define a Python function add(a, b) that returns a + b."
```

## CLI

```
python main.py [--task-id ID] [--pipeline-log PATH] [--cost-budget USD]
               [--max-task-chars N] [--no-injection-guard] TASK
```

Exit codes: `0` SUCCESS, `1` FAIL/BLOCKED, `2` usage error.

## Project adapters

`core.adapter.ProjectAdapter` describes a concrete external project: language, additional protected paths, forbidden tokens, and named commands (`test`, `lint`, `build`) with `argv`-tuples (no shell strings). `AdapterRegistry` holds many adapters, so one orchestrator instance can serve multiple projects in parallel.

```python
from pathlib import Path
from core.adapter import ProjectAdapter, ProjectCommand, AdapterRegistry

reg = AdapterRegistry()
reg.register(ProjectAdapter(
    name="my_app",
    project_path=Path("/path/to/project"),
    language="python",
    commands={
        "test": ProjectCommand(name="test", cmd=("pytest", "-q")),
        "lint": ProjectCommand(name="lint", cmd=("ruff", "check", ".")),
    },
    forbidden_paths=(".env", "secrets/"),
    forbidden_tokens=("AWS_SECRET", "DROP TABLE"),
))
```

## FSM

States: `IDLE → PLANNING → PM → ARCHITECT → WRITER → REVIEW → TEST → QA → SUCCESS`. Failure paths: `BLOCKED` (preparatory states), `FAIL` (post-WRITER states). Repair loops: `REVIEW ↔ FIX`, `QA → FIX → REVIEW → TEST → QA`. Limits and full transition table are in `docs/fsm_spec.md` and enforced in `core/fsm.py`.

## Safety

- All agent calls go through an injected `AgentRegistry` — orchestrator never touches the network directly.
- `core.contracts` blocks edits to protected files (`agents.py`, `fsm.py`, `contracts.py`) and rejects `TODO`/`FIXME`/`NotImplementedError`/`placeholder` tokens in agent-written code.
- `core.patcher.apply_change` writes atomically (`tempfile.mkstemp` + `os.replace`).
- `core.git_integration` blocks `reset --hard` on `main`/`master` without explicit `force=True`, refuses paths escaping the repo, and never emits `--force`/`--no-verify`/`--no-edit`.
- `core.adapter.ProjectCommand` rejects shell metacharacters in `argv` tokens.
- `Orchestrator` accepts `task_validators=` and ships `reject_long_task` + `reject_injection_markers` for prompt-injection defence.
- `Orchestrator` accepts `cost_budget_usd=` — pipeline terminates FAIL on exceed.

## Observability

`core.observability.Observability` records frozen `LogRecord` / `MetricSample` / `AgentCallRecord` to a pluggable `LogSink`. `JsonLinesSink` appends to a JSONL file (locked, atomic per record). Query API on `InMemorySink`: `cost_snapshot(task_id?)`, `agent_performance(name)` with avg + p50/p95/p99 latency.

## Quality gates

`scripts/quality_check.sh` and `core.quality_gates.QualityGates` run ruff + pytest + coverage with a configurable minimum coverage threshold. The same `QualityGates` class can be wired into a CI workflow.

## Running the e2e test

The full pipeline e2e test exercises real OpenRouter API calls, a real git worktree,
and real ruff/pytest validation. It uses the ECONOMY tier and costs up to ~$0.20 per run.

```bash
AI_DEV_TEAM_REAL_LLM=1 OPENROUTER_API_KEY=sk-or-v1-... \
    pytest tests/integration/test_real_pipeline_e2e.py -v -s
```

The test creates an isolated tmp git repo — it does **not** touch your project.
It skips automatically in CI when `OPENROUTER_API_KEY` or `AI_DEV_TEAM_REAL_LLM=1` are absent.

## License

MIT — see [LICENSE](LICENSE).
