# 0039 — Shared immutable extra-addons checkouts

**Status:** Adopted
**Type:** Architecture
**First introduced:** this change (2026-07-22)
**Key code:** `extra_addons.py` (SHA checkout cache + repo locks), `docker_ops/env_ops.py` (dev mount lifecycle and lazy migration)

## Context

The original extra-addons design ([[0010-extra-addons-repositories]]) already
shared one bare Git object store per team, but materialised a complete worktree
inside every environment. Teams commonly run many environments against the same
Enterprise or OCA branch, so identical unpacked files and inodes still consumed
disk once per environment.

A single mutable checkout per branch would save that space but break environment
isolation. Branches move, and `pull_and_apply` for one environment would expose
new files immediately to every other container sharing the checkout, without
upgrading their databases or restarting their Odoo registries. Production also
needs private worktree HEADs for independent deploy rollback
([[0035-production-hosting]]).

## Decision

Development environments share **persistent immutable checkouts keyed by the
resolved commit SHA**, not by branch. A checkout lives under the team's
`shared_extra_checkouts/<repo>/<sha>` cache and is mounted read-only into every
development environment pinned to that revision.

- The requested branch remains the user-facing selector; Oduflow fetches it and
  records the resolved SHA in the environment's Docker labels.
- Moving a branch creates or reuses a different SHA checkout. Existing
  environments keep their old mount until their own `pull_and_apply`.
- Cached checkouts are not reference-counted and are not deleted with an
  environment. Deleting the owning extra repo removes all of its revisions.
- Production keeps per-production mutable worktrees so its existing deploy log
  and rollback semantics remain independent.

## How it works

Creating an environment resolves every configured extra-addons branch, ensures
the SHA checkout exists, and mounts it at the existing
`/mnt/extra-addons-<name>` path. Operations that fetch, create, remove, or reset
worktrees are serialized by a team/repo lock while unrelated extra repos remain
parallel.

During `pull_and_apply`, Oduflow compares the mounted SHA with the current branch
tip and classifies the Git diff against the new immutable checkout. After strict
guardrails accept the action, it recreates only that environment's container
with the new read-only bind while preserving its database and filestore, then
runs the normal install/upgrade/restart action. Existing per-environment
worktrees are migrated lazily on their next sync and removed after a successful
mount switch.

## Consequences

- Environments on the same dependency revision pay for one checkout while
  retaining independent update timing and database state.
- Old revisions accumulate by design until the extra repo is deleted. This
  avoids reference accounting and makes rollback/reproducibility predictable;
  explicit cache pruning can be added later if operational data warrants it.
- An extra-addons revision change requires a transparent container recreation
  to change the host bind source. Database, filestore, ports, labels, and the
  public container path remain stable.
- Production does not receive the disk optimization yet; extending it requires
  a separate immutable-mount rollback design.

## Evolution

This decision supersedes only the **per-development-environment worktree** part
of [[0010-extra-addons-repositories]]. Its shared bare clones, explicit branches,
read-only mounts, protection, and deletion dependency guards remain in force.

## History

- 2026-07-22 — adopt team-shared immutable SHA checkouts for development
  environments, with lazy migration and persistent cache lifecycle.
