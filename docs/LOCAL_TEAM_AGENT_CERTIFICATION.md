# Local Team / Agent Certification

Checked on `2026-05-18`.

Canonical local pilot runbook:
- `docs/LOCAL_ISOLATED_PILOT_RUNBOOK.md`

Canonical local pilot status:
- `docs/LOCAL_ISOLATED_PILOT_STATUS.md`

## Outcome

`team certification partially blocked`

## Scope

This document certifies the existing team and agent model inside the isolated
local pilot contour from `L0.1`.

It does **not**:

- invent new roles
- convert every logical role into a separate Telegram bot identity
- start `L0.3` UI polish
- start `L0.4` pilot task execution
- attach AI Dev Team to the live Docker-mounted main project

## Current Known Role Catalog

The current known role catalog is defined by `core/agent_role_catalog.py`.

Known roles:

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
- `data_agent`

## Baseline Internal Team

The baseline internal team is the always-known internal project team used by
the current coordinator assembly flow:

- `coordinator_agent`
- `planning_agent`
- `pm_agent`
- `architect_agent`
- `writer_agent`
- `reviewer_agent`
- `tester_agent`
- `qa_agent`
- `fixer_agent`

Current assembled-team truth:

- assembly mode is `baseline_internal_team`
- `captain_role` is `coordinator_agent`
- assembled member order is stable and matches the baseline internal team order
- the assembled baseline roster is what `/agents` presents as the active team
  when project context is resolved

## Specialist Roles

Known specialist roles are first-class logical backend roles:

- `security_agent`
- `devops_agent`
- `data_agent`

Current specialist truth:

- specialist roles exist in the canonical role catalog
- specialist roles have personas
- specialist roles can be approved into per-project specialist rosters
- specialist roles can appear in resolved project team membership
- specialist roles are not part of the baseline assembled internal roster

## Persona Coverage

`core/agent_personas.py` provides persona coverage for every known role.
`tests/test_agent_personas.py` verifies that `DEFAULT_PERSONAS` covers
`KNOWN_AGENT_ROLES`.

Default persona coverage includes:

- `coordinator_agent` → `Координатор`
- `planning_agent` → `Планировщик`
- `pm_agent` → `Менеджер`
- `architect_agent` → `Архитектор`
- `writer_agent` → `Программист`
- `reviewer_agent` → `Ревьюер`
- `tester_agent` → `Тестировщик`
- `qa_agent` → `QA-инженер`
- `fixer_agent` → `Фиксер`
- `security_agent` → `Безопасник`
- `devops_agent` → `Девопс`
- `data_agent` → `Дата-инженер`

Persona existence means the role is named and characterized for prompts and
presentation. It does **not** by itself prove a separate live Telegram bot
identity.

## Selectable Logical Roles

The current selectable logical role set is broader than the runtime-exposed
identity set.

Selectable logical roles include:

- baseline worker-oriented roles:
  `planning_agent`, `pm_agent`, `architect_agent`, `writer_agent`,
  `reviewer_agent`, `tester_agent`, `qa_agent`, `fixer_agent`
- specialist roles:
  `security_agent`, `devops_agent`, `data_agent`

Current truth:

- `coordinator_agent` is known and runtime-exposed, but it is intentionally not
  part of `SELECTABLE_AGENT_ROLE_ORDER`
- selectable/logical existence does not imply separate Telegram exposure

## Runtime-Exposed Roles

The current runtime-exposed roles are the baseline internal team roles only:

- `coordinator_agent`
- `planning_agent`
- `pm_agent`
- `architect_agent`
- `writer_agent`
- `reviewer_agent`
- `tester_agent`
- `qa_agent`
- `fixer_agent`

This is the truthful current meaning of runtime exposure at `L0.2`:

- these roles are the identities the current runtime/control-plane semantics
  are built around
- they back the assembled baseline team shown in `/agents`
- they are the only roles currently classified as runtime-exposed in
  `core/agent_role_catalog.py`

## Logical-Only Roles

The current logical-only roles are the specialist roles:

- `security_agent`
- `devops_agent`
- `data_agent`

Logical-only means:

- the role exists in the backend catalog
- the role has a persona
- the role can be selected/consulted logically
- the role can be represented in project specialist state
- the role is not yet separately certified here as a live Telegram bot identity

This step does **not** certify that all logical roles are already fully
separate Telegram bot identities.

## Assembled Team and Project-Team Semantics

The current assembled team semantics are split across two truthful layers.

Layer 1: active assembled baseline team

- the coordinator assembles the baseline internal team only
- context sources currently supported for truthful active assembly are:
  `bound_chat` and `owner_dm_single_project`
- `/agents` shows `Текущая assembled team` only when project context is
  resolved truthfully
- when project context is not resolved, `/agents` shows the
  `Baseline internal team template` as a reference template instead of
  pretending there is an active assembled team

Layer 2: persisted project team state

- approved specialists live in `ProjectSpecialistRoster`
- resolved project team roles are:
  baseline internal team + approved specialist roles
- pending hire requests stay outside resolved membership until approved

This is the key distinction for operators:

- assembled runtime team ≠ all logical known roles
- persisted project team state ≠ separate live Telegram identity exposure

## Operator-Visible Surfaces

Current operator-visible team and agent surfaces already expose truthful parts
of the model.

### `/agents`

Evidence from `tests/test_bot_runner.py` and
`tests/integration/test_coordinator_flow.py` confirms:

- `/agents` shows `Baseline internal team template` and `reference template`
  when no project context is resolved
- `/agents` shows `Текущая assembled team`,
  `context_source`, and `captain_role: coordinator_agent` when project context
  is resolved
- `/agents` can include `Project specialists:` as a separate block
- `/agents` does not falsely claim that every specialist role is already an
  active Telegram identity

### Web Office project overview

`/projects/{project_id}` exposes a truthful team summary:

- approved specialists count
- pending hire requests count
- resolved team size
- previews for approved specialists and pending hire requests

### Web Office team page

`/projects/{project_id}/team` exposes persisted logical team state only:

- baseline internal team
- approved specialist roster
- pending hire requests
- resolved team roles

The page already says that approved specialists and pending hire requests do
not imply runtime activation.

### Team API serialization

`GET /api/projects/{project_id}/team` and the internal serializer in
`web/main.py` expose:

- `baseline_internal_team_roles`
- `project_specialist_roster`
- `resolved_team_roles`
- `pending_hire_requests`

## Local Certification Evidence

Code-level evidence used for this certification:

- `core/agent_role_catalog.py`
- `core/agent_personas.py`
- `core/coordinator_team_assembly.py`
- `core/project_team_state.py`
- `web/main.py`
- `web/templates/project.html`
- `web/templates/project_team.html`

Existing test evidence used for this certification:

- `tests/test_agent_personas.py`
- `tests/test_coordinator_team_assembly.py`
- `tests/test_dispatcher_agents.py`
- `tests/test_project_registry.py`
- `tests/test_web_project_team_view.py`
- `tests/test_bot_runner.py`
- `tests/integration/test_coordinator_flow.py`

Local pilot evidence boundary from `L0.1`:

- isolated Web Office, `/healthz`, `/readyz`, and backup proof were already
  verified locally
- dedicated isolated Telegram pilot credentials are still missing

## What Is Verified

- the known role catalog is explicit and stable
- the baseline internal team is explicit and stable
- specialist roles are explicit and stable
- persona coverage exists for all known roles
- runtime-exposed roles are clearly limited to the baseline internal team
- logical-only specialist roles are clearly separated from runtime exposure
- assembled team semantics are truthful and bounded
- `/agents` and Web Office team/project surfaces expose the existing model
  without pretending all roles are already fully live Telegram identities

## What Is Still Blocked

The remaining blocked surface is bounded and specific:

- full live Telegram proof inside the isolated pilot still needs dedicated
  `TELEGRAM_OWNER_CHAT_ID`
- full live Telegram proof for single-bot compatibility still needs a dedicated
  isolated pilot `TELEGRAM_BOT_TOKEN`
- full live Telegram proof for multi-bot startup still needs dedicated isolated
  pilot `TELEGRAM_AGENT_TOKENS`

Because those dedicated isolated pilot credentials are still missing, this step
does not claim complete live Telegram identity proof for every runtime-exposed
baseline role.

## Handoff to `L0.3`

`L0.3` should build on this certification by checking that the existing Web
Office surfaces communicate the already-verified team model clearly and
consistently.

That next step should focus on:

- UI clarity of the current team/project surfaces
- truthful visibility of baseline vs specialists vs pending requests
- certification of operator-facing wording, not role-model expansion

## What This Step Intentionally Did Not Do

- create new agent roles
- change role catalog semantics
- expose specialist roles as new Telegram bot identities
- start a pilot task on a sandbox repo
- enable assist-mode against the live main project
- start VPS rollout or server bootstrap work
