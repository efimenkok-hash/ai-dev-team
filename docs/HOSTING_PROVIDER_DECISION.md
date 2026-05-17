# Hosting Provider Decision for `C1.1`

Checked on `2026-05-17`.

This document is the canonical decision record for `C1.1` under the new
architecture. It fixes the hosting substrate choice for the current
multi-project, multi-bot, Web Office runtime and prepares a truthful handoff to
`C1.2`.

Navigation:

- current pre-hosting operator runbook:
  `docs/DEPLOY_NEW_ARCHITECTURE.md`
- active production roadmap:
  `docs/ROADMAP_TO_PRODUCTION.md`

This step is about provider selection and purchase readiness only. It does not
claim that Ubuntu bootstrap, `nginx`, `systemd`, bot deployment, Web Office
deployment, domain wiring, HTTPS, server-side backups, or server-side health
monitoring are already done.

## Decision Summary

- chosen provider: `Hetzner Cloud`
- chosen plan / SKU: `CPX22` (`Regular Performance`)
- chosen region: `hel1` (`Helsinki, Finland`)
- recommended OS baseline for `C1.2`: `Ubuntu 24.04 LTS`
- public networking decision: add one `Cloud Primary IPv4`
- expected monthly cost on the checked date:
  - `CPX22`: `EUR 7.99/mo` in the Germany/Finland price group
  - one `Cloud Primary IPv4`: `EUR 0.50/mo`
  - expected total: `EUR 8.49/mo` excluding VAT
- purchase status: `not purchased yet`
- blocker: purchase is blocked externally because this workspace has no access
  to a real provider account, billing instrument, or provider-console approval
  path

## Current Architecture Fit

The selected VPS only needs to host the architecture that already exists in the
repository today:

- Telegram runtime via `scripts/run_telegram_bot.py`
- Web Office via `web.main:app`
- one shared persisted SQLite `state.db`
- local startup validation, local health model, and local backup primitive
- local backup CLI via `scripts/backup_state_db.py`
- no managed database requirement
- no Kubernetes or managed PaaS requirement

The current hosting criteria for `C1.1` are therefore:

- enough headroom for the Telegram runtime, Web Office, SQLite state, and local
  backup artifacts on one Ubuntu VPS
- straightforward SSH/root access
- predictable monthly price
- no forced managed database or managed container platform
- clean handoff into a manual Ubuntu-based rollout in `C1.2`

## Shortlist Compared

The shortlist was intentionally kept narrow and practical. All values below are
from official provider pages checked on `2026-05-17`.

| Provider | Exact plan / SKU | Region | vCPU | RAM | Disk | Public IPv4 situation | Monthly price | One-time setup fee on checked official pages | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Hetzner Cloud | `CPX22` (`Regular Performance`) | `hel1` | 2 | 4 GB | 80 GB SSD | not included in the server price; add one `Cloud Primary IPv4` separately | `EUR 7.99/mo` + `EUR 0.50/mo` for IPv4, excl. VAT | none shown on the checked official pages | best price/performance fit for the current architecture |
| DigitalOcean | `Basic Regular Droplet` (`4 GiB / 2 vCPU`) | `fra1` | 2 | 4 GiB | 80 GiB SSD | public networking enabled by default for Droplets | `USD 24.00/mo` | none shown on the checked official pages | simple operator model, but much more expensive at the same size class |
| Vultr | `Cloud Compute` (`vc2-2c-4gb`) | `ams` | 2 | 4 GB | 100 GB SSD | public IPv4 enabled by default | `USD 24.00/mo` | none shown on the checked official pages | viable fallback, but materially more expensive than Hetzner for this rollout |

## Why Hetzner Won

`Hetzner Cloud CPX22` won for the current architecture because it fits the real
runtime without paying for growth that is not needed yet:

1. It matches the right size class.
   - `2 vCPU + 4 GB RAM` is a reasonable baseline for one Telegram runtime, one
     Web Office process, shared SQLite state, and local backup creation on a
     single VPS.
2. It gives enough disk for the current storage model.
   - `80 GB` is materially more comfortable than the lower Hetzner `CX23`
     `40 GB` option once `state.db`, work trees, logs, and local backup
     artifacts all live on the same machine.
3. It stays operationally simple.
   - plain VPS, SSH/root access, Ubuntu support, no managed DB, no container
     platform, no hidden architectural assumption beyond the existing repo.
4. Its price is far better for this step than the shortlist alternatives.
   - even before FX normalization, `EUR 8.49/mo` excluding VAT with a public
     IPv4 is substantially below the comparable `USD 24.00/mo` DigitalOcean and
     `USD 24.00/mo` Vultr options.
5. `hel1` is the right region for this rollout.
   - it stays in the lower Germany/Finland Hetzner pricing group and is
     geographically sensible for the current Europe-based operator path.

## Why the Other Shortlist Candidates Did Not Win

### DigitalOcean

DigitalOcean remains operationally clean and fully viable, but it lost on cost.
The official `Basic Regular` `4 GiB / 2 vCPU` Droplet in `fra1` is the right
shape technically, yet it is priced at `USD 24.00/mo`, which is much higher
than the current Hetzner choice for the same deployment step.

### Vultr

Vultr also remains viable and has a clean shared-CPU path with Ubuntu 24.04 and
public IPv4 enabled by default. It still loses for the current rollout because
the official `vc2-2c-4gb` class sits at `USD 24.00/mo`, again materially above
the Hetzner decision without giving a decisive architectural advantage for this
repo.

## Exact Chosen Spec

The purchase-ready decision for `C1.1` is:

- provider: `Hetzner Cloud`
- plan / SKU: `CPX22`
- plan family: `Regular Performance`
- region: `hel1` (`Helsinki, Finland`)
- OS baseline for the next step: `Ubuntu 24.04 LTS`
- network baseline:
  - one `Cloud Primary IPv4`
  - optionally keep the default free IPv6 available
- expected monthly cost on `2026-05-17`:
  - `EUR 7.99/mo` for `CPX22`
  - `EUR 0.50/mo` for one `Cloud Primary IPv4`
  - total `EUR 8.49/mo`, excluding VAT

## Purchase Status

Current truth state: `purchase blocked externally`.

The server has **not** been purchased from this workspace. The blocker is not a
technical uncertainty about provider choice; the blocker is external account and
payment access:

- no live Hetzner Cloud account session is available here
- no billing instrument is available here
- no manual provider-console approval path is available here

This means `C1.1` is complete as a **purchase-ready hosting decision**, not as a
falsely claimed completed purchase.

## Handoff to `C1.2`

`C1.2` should start from the following exact server target:

- provider: `Hetzner Cloud`
- server plan: `CPX22`
- region: `hel1`
- OS image baseline: `Ubuntu 24.04 LTS`
- public networking: one `Cloud Primary IPv4`

Before `C1.2` can begin, one manual operator action is still required:

1. Sign in to a real Hetzner Cloud account.
2. Purchase one `CPX22` server in `hel1`.
3. Select `Ubuntu 24.04 LTS`.
4. Attach or create one `Cloud Primary IPv4`.
5. Record the server facts needed by the next step:
   - server name
   - server ID
   - public IPv4
   - SSH access method

Once that exists, `C1.2` can truthfully proceed with Ubuntu package bootstrap,
`gh`, `nginx`, and `systemd`.

## What This Step Explicitly Did Not Do

This step did **not**:

- bootstrap Ubuntu
- install packages
- configure `nginx`
- configure `systemd`
- deploy the Telegram runtime
- deploy the Web Office runtime
- connect a domain
- configure HTTPS
- enable server-side backup automation
- enable server-side health monitoring

## Official Sources Used

Hetzner:

- [Hetzner Cloud Regular Performance](https://www.hetzner.com/cloud/regular-performance)
- [Hetzner Price Adjustment](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/)
- [Hetzner Cloud Server Overview](https://docs.hetzner.com/cloud/servers/overview/)
- [Hetzner Cloud Locations](https://docs.hetzner.com/cloud/general/locations/)
- [Hetzner Primary IP Overview](https://docs.hetzner.com/cloud/servers/primary-ips/overview)

DigitalOcean:

- [DigitalOcean Droplet Pricing](https://www.digitalocean.com/pricing/droplets)
- [DigitalOcean Regional Availability](https://docs.digitalocean.com/platform/regional-availability/)
- [DigitalOcean Linux Images for Droplets](https://docs.digitalocean.com/products/droplets/details/images/)
- [DigitalOcean Droplets API Reference](https://docs.digitalocean.com/products/droplets/reference/api/droplets/)

Vultr:

- [Vultr Pricing](https://www.vultr.com/pricing/)
- [How to Provision Vultr Cloud Compute Instances](https://docs.vultr.com/products/compute/cloud-compute/provisioning)
- [Vultr Cloud Compute Overview](https://docs.vultr.com/products/compute/cloud-compute)
- [Vultr Datacenter Locations](https://www.vultr.com/features/datacenter-locations/)
