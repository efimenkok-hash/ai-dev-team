# VPS Baseline Bootstrap

Checked on `2026-05-18`.

Canonical hosting decision:
- `docs/HOSTING_PROVIDER_DECISION.md`

Canonical pre-hosting deploy guide:
- `docs/DEPLOY_NEW_ARCHITECTURE.md`

Canonical production roadmap:
- `docs/ROADMAP_TO_PRODUCTION.md`

## Outcome

`bootstrap blocked externally`

## Scope

This document records the truthful result of `C1.2`.

This step is only about machine-level VPS bootstrap:

- confirm the real server exists and is reachable
- confirm exact server facts against `C1.1`
- confirm or install Ubuntu baseline tooling
- prepare a handoff into `C1.3`

This step does **not**:

- deploy the Telegram runtime
- deploy the Web Office runtime
- upload app `.env`
- run `gh auth login`
- create app-specific systemd units
- configure final nginx site routing
- configure domain or HTTPS
- enable server-side backup automation
- enable server-side healthcheck rollout

## Target Server Facts From `C1.1`

The chosen purchase-ready target from `docs/HOSTING_PROVIDER_DECISION.md` is:

- provider: `Hetzner Cloud`
- plan / SKU: `CPX22`
- region: `hel1`
- OS baseline target: `Ubuntu 24.04 LTS`
- networking baseline: `1 x Cloud Primary IPv4`
- expected checked monthly cost on `2026-05-17`: `EUR 8.49/mo` excl. VAT

These remain the canonical target facts for `C1.2`.

## Actual Server Facts Observed In This Step

No real purchased server facts could be confirmed from the current workspace.

Unavailable in current evidence:

- server name
- server ID
- public IPv4
- SSH hostname or alias
- confirmed root access
- confirmed sudo access

Because those facts are unavailable, this step cannot truthfully claim that a
real VPS already exists and matches `C1.1`.

## Precondition Checks Performed

The following checks were performed before attempting any bootstrap:

1. Re-read `docs/HOSTING_PROVIDER_DECISION.md`
   - current canonical truth still says:
     - purchase status: `not purchased yet`
     - purchase state: `purchase blocked externally`
     - manual provider-console purchase is still required before `C1.2`
2. Re-read `docs/DEPLOY_NEW_ARCHITECTURE.md` and
   `docs/ROADMAP_TO_PRODUCTION.md`
   - no newer canonical purchased-server facts are recorded there
3. Checked the repo for real server facts
   - no canonical server name
   - no server ID
   - no public IPv4
   - no exact SSH target recorded in repo docs
4. Checked local SSH operator hints
   - `~/.ssh/config` is not present in this workspace session
   - therefore no configured `Host` alias / `HostName` path for the chosen VPS
     was available here

## Verification Commands And Compact Results

Local evidence commands used in this step:

- `sed -n '1,260p' docs/HOSTING_PROVIDER_DECISION.md`
  - confirmed `Hetzner Cloud`, `CPX22`, `hel1`, `Ubuntu 24.04 LTS`,
    `1 x Cloud Primary IPv4`, and `not purchased yet`
- `sed -n '1,260p' docs/DEPLOY_NEW_ARCHITECTURE.md`
  - confirmed deploy guide still treats VPS bootstrap as not yet completed
- `sed -n '214,270p' docs/ROADMAP_TO_PRODUCTION.md`
  - confirmed roadmap phase still expects `C1` manual rollout work
- `rg -n "Hetzner|CPX22|hel1|IPv4|server id|server name|ssh" docs README.md tests`
  - found only decision/docs references, not real purchased-server facts
- `test -f ~/.ssh/config && rg -n "^(Host|HostName|User)\\b|hetzner|ai-dev-team|aidt" ~/.ssh/config || true`
  - produced no host mapping because `~/.ssh/config` is absent in this session

No remote verification commands could be run because there was no public IPv4,
SSH hostname, or provider-console target available to connect to.

## Bootstrap Result By Component

### Ubuntu

- target required by `C1.1`: `Ubuntu 24.04 LTS`
- actual remote OS: not verified
- reason: no confirmed reachable VPS target exists in the current workspace

### Python

- target package surface: `python3`
- remote verification: not run
- reason: no SSH-accessible server facts available

### `python3-venv`

- remote verification: not run
- reason: no SSH-accessible server facts available

### `nginx`

- remote installation and `nginx -t`: not run
- reason: no SSH-accessible server facts available

### `gh`

- remote installation and `gh --version`: not run
- reason: no SSH-accessible server facts available

### `systemd`

- remote verification via `systemctl --version`: not run
- reason: no SSH-accessible server facts available

## Blocker

Current truthful blocker:

- `C1.1` target server purchase still cannot be confirmed from this workspace
- no public IPv4 is available here
- no SSH target is available here
- no root/sudo path is available here
- therefore no machine bootstrap commands can be executed truthfully

This is an external blocker, not a hidden in-repo runtime problem.

## Handoff To `C1.3`

`C1.3` must **not** start yet.

Before `C1.3` can begin, the following manual precondition must be satisfied:

1. Sign in to a real Hetzner Cloud account.
2. Create the canonical target server:
   - `CPX22`
   - `hel1`
   - `Ubuntu 24.04 LTS`
   - `1 x Cloud Primary IPv4`
3. Record and provide the real server facts:
   - server name
   - server ID
   - public IPv4
   - SSH access method
   - root or working sudo path
4. Re-run `C1.2` so Ubuntu / Python / `python3-venv` / `nginx` / `gh` /
   `systemd` can be verified on the actual machine.

Only after that may `C1.3` proceed with app-level rollout work.

## What Is Ready For `C1.3`

The following preparation is already real:

- canonical provider choice is fixed
- exact SKU / region / OS target do not drift from `C1.1`
- current blocker is explicit
- next manual action is explicit

## What Is Intentionally Still Not Done

This step does **not** claim that the following are already done:

- bot backend deployed
- Web Office deployed
- app `.env` installed on the server
- app-specific systemd units created
- nginx reverse proxy configured for the app
- domain connected
- HTTPS enabled
- server-side backup automation enabled
- server-side healthchecks enabled
