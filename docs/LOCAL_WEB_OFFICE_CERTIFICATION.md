# Local Web Office Certification

Checked on `2026-05-18`.

Canonical local pilot runbook:
- `docs/LOCAL_ISOLATED_PILOT_RUNBOOK.md`

Canonical local pilot status:
- `docs/LOCAL_ISOLATED_PILOT_STATUS.md`

Canonical local team / agent certification:
- `docs/LOCAL_TEAM_AGENT_CERTIFICATION.md`

## Outcome

`Web Office certified locally`

## Local Certification Scope

This step certifies the current Web Office as a truthful local operator surface
inside the isolated no-Docker pilot contour.

It certifies:

- dashboard usability
- project overview readability
- team/history/settings visibility
- `/healthz` and `/readyz` readability for local operator checks
- navigation clarity across the existing SSR pages
- empty vs happy state truthfulness

It does **not** certify:

- VPS or production serving
- domain or HTTPS rollout
- live websocket operator UI
- SPA behavior
- pilot task execution on a sandbox repo
- attach to the live Docker-mounted main project

## Local Evidence Setup

Two isolated localhost surfaces were used for this certification:

Happy-path seeded Web Office:

- bind: `127.0.0.1:8001`
- state DB:
  `/private/tmp/ai-dev-team-web-ui-cert-3/state/state.db`
- seeded projects:
  - `alpha_project`
  - `beta_project`

Empty-dashboard Web Office:

- bind: `127.0.0.1:8002`
- state DB:
  `/private/tmp/ai-dev-team-web-ui-cert-empty/state/state.db`

The localhost binds required sandbox approval in this Codex session. That is a
tooling detail of the local certification environment, not a product blocker.

## Surfaces Checked

The following operator surfaces were checked:

- dashboard: `/`
- project view: `/projects/{project_id}`
- team view: `/projects/{project_id}/team`
- history view: `/projects/{project_id}/history`
- settings view: `/projects/{project_id}/settings`
- health surfaces:
  - `/healthz`
  - `/readyz`

The following backend/operator surfaces were intentionally **not** treated as a
local Web Office UI deliverable in this step:

- `/ws/events` as a live operator console
- a realtime websocket client
- any production hosting/admin console

## Navigation Truth

Navigation is locally certified as truthful and sufficient for the current
operator contour.

Verified:

- dashboard project cards link only to canonical project routes
- project view has a clear `Back to dashboard` path
- project view exposes explicit links to:
  - team page
  - history page
  - settings page
- team/history/settings pages expose `Back to project`
- page titles and hero labels make it clear which surface is summary vs detail
- project identity remains visible through:
  - project name
  - project ID
  - project status

This is already sufficient for `L0.4`; no IA rewrite or tab shell was needed.

## Empty State Truth

Empty states were certified as honest.

Dashboard empty state on `127.0.0.1:8002`:

- shows `No persisted projects yet.`
- explicitly says the page is wired to the real project registry
- does not inject fake sample cards

Project-level empty states on `beta_project`:

- project overview:
  - `Policy missing`
  - `Chat unbound`
  - `Runtime unbound`
  - `No approved specialists yet.`
  - `No pending hire requests.`
  - `No persisted task history yet.`
  - `No persisted threads yet.`
- team view:
  - `No approved specialists yet.`
  - `No pending hire requests.`
- history view:
  - `No persisted task history yet.`
  - `Latest final state` becomes `No history`
  - `Latest branch` becomes `None`
- settings view:
  - `Policy missing`
  - `Chat binding missing`
  - `Runtime binding missing`
  - no fake booleans, adapters, or chat values

No fake sample activity was observed.
No fake live updates were observed.
No fake team members or fake settings values were observed.

## Happy Path Truth

Happy-path usability was certified on `alpha_project`.

Dashboard:

- renders both persisted projects clearly
- binding pills distinguish wired vs missing state
- project cards give a clean entry point into real project pages

Project overview:

- identity block is easy to read
- summary cards separate:
  - team state
  - recent tasks
  - persisted threads
- links into team/history/settings are obvious and bounded

Team view:

- baseline internal team
- approved specialists
- pending hire requests
- resolved team roles

The page remains explicit that persisted logical team state does not imply
runtime activation.

History view:

- recent tasks render project-scoped only
- no thread/runtime leakage into history copy
- latest task metrics are locally readable
- recent tasks now render recent-first by `finished_at`

Settings view:

- policy presence vs missing is obvious
- chat/runtime binding presence vs missing is obvious
- read-only nature is explicit
- no false editability or runtime health claims appear

Health surfaces:

- `/healthz` and `/readyz` return compact raw JSON that is readable enough for
  local pilot checks
- they remain local-only health/readiness outputs, not a live monitor UI

## Operator Usability Findings

Current Web Office/operator contract is already usable locally because it keeps
the UI bounded and truthful.

Certified strengths:

- summary pages and detailed pages are easy to distinguish
- page copy consistently explains what the surface is showing
- missing state is represented as missing, not as fallback demo data
- settings page stays read-only and does not pretend to be an admin console
- team page gives the clearest truthful separation of baseline, approved,
  pending, and resolved state

## What Was Minimally Improved

One small blocking UI truth issue was fixed during this certification:

- recent task summaries in HTML views are now sorted by `finished_at`
  descending before rendering

Why this was needed:

- the HTML project overview preview and history view could otherwise reflect
  insertion order instead of actual latest-finished task order
- that made `Latest final state` and recent-task previews potentially misleading
  for local operators when persisted writes arrived out of chronological order

What was **not** changed:

- no backend API contracts were changed
- no state model semantics were changed
- no history API shape was expanded
- no websocket/live-update feature was added

## What Is Still Intentionally Not Implemented

This certification does **not** claim any of the following:

- production hosting is already configured
- VPS rollout is complete
- systemd is already configured for the app
- nginx/HTTPS is already configured for the app
- a live realtime operator console already exists
- `/ws/events` is already surfaced as a local live dashboard
- direct attach to the live Docker-mounted main project is enabled

## Handoff to `L0.4`

`L0.4` can now use the current Web Office as a truthful local operator surface
for a sandbox-repo pilot task.

That next step can assume:

- dashboard/project/team/history/settings navigation is usable
- empty states remain honest
- local health/readiness checks are readable
- recent-task summaries no longer mislead on out-of-order persisted writes

`L0.4` should build on this certified operator surface instead of redesigning
it.
