# 0010 — Extra addons repositories (bare clones + per-environment worktrees)

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `d495dd8` "extra addons repos support" (2026-02-13)
**Key code today:** `extra_addons.py` (bare clone / worktree lifecycle, fetch summaries), `docker_ops/env_ops.py` (worktree creation + read-only mount, addons_path generation), `server.py` (`add_extra_repo` / `list_extra_repos` / `update_extra_repo` / `delete_extra_repo`)

## Context

An environment serves the developer's **primary** repository
([[0001-mcp-orchestrated-ephemeral-per-branch-environments]]), but real Odoo
modules rarely stand alone. A module under test commonly depends on code that
lives in *other* repositories — Odoo Enterprise, OCA addon collections, a
company's shared base modules. Without a way to bring those into the
environment, the module won't install and tests can't run.

Forces at play:
- A team has a **small set of shared addon repos** reused across many branches
  and many environments. Cloning each one fully, per environment, would waste a
  lot of disk and time, and they recreate often.
- Different environments may need **different branches** of the same addon repo
  (e.g. an `18.0` env and a `19.0` env pointing at the same Enterprise repo).
  The addon checkout therefore must be **pinned per environment**, not global.
- These repos are usually private, so cloning has to reuse the team's existing
  git credentials (`git_ops` / `setup_repo_auth`).

## Decision

Manage extra addon repositories as **team-shared bare clones**, and give each
environment its own **git worktree** of the bare repo, checked out at an
**explicitly specified branch**. The bare clone is the single on-disk copy of
the repo's objects; worktrees are cheap, branch-pinned views onto it that are
bind-mounted **read-only** into the container.

- **Bare clone per repo, shared by the team.** `add_extra_repo(name, repo_url)`
  does a `git clone --bare` into the team's shared repos directory. There is one
  object store per addon repo regardless of how many environments use it.
- **Worktree per environment, pinned to a branch.** On environment create,
  Oduflow adds a detached worktree at the requested branch and mounts it at
  `/mnt/extra-addons-<name>`; the generated `odoo.conf` appends that path to
  `addons_path`. Disk cost per environment is roughly one checkout, not a full
  clone.
- **Branch is mandatory and explicit.** Extra addons are requested as
  `name:branch` (e.g. `enterprise:18.0`). A missing branch is a hard error — no
  silent fallback to a default branch — because addon repos branch by Odoo
  version and guessing wrong produces a confusing "module not found".
- **Protect / unprotect.** A repo can be marked protected (a `.protected`
  marker), which blocks deletion until it is explicitly unprotected. Deletion is
  also refused while any environment still depends on the repo.
- **Fetch with a summary, and propagate to environments.** `update_extra_repo`
  fetches the bare clone and reports *what changed* (new / deleted / updated
  branches with commit counts); `pull_and_apply` refreshes an environment's
  extra-addon worktrees alongside the main repo so addon updates reach running
  environments through the normal apply path.

## How it works (macro)

- **Add once, reuse everywhere.** The team adds an addon repo once; every
  environment that lists it gets a worktree, so the shared object store is
  fetched once and amortised across branches and users.
- **Branch-pinned, read-only mounts.** Each environment's worktree is detached
  at its branch tip and mounted read-only — the environment consumes the addon
  code but never mutates the shared repo. Worktrees are pruned/removed with the
  environment.
- **Explicit refspec for bare clones.** `git clone --bare` does not configure a
  fetch refspec, so a later `git fetch --all` would only move `FETCH_HEAD` and
  leave `refs/heads/*` stale. Oduflow sets `+refs/heads/*:refs/heads/*`
  explicitly after clone, and auto-migrates older bare repos that lack it before
  fetching, so updates actually land on local branches.
- **Updates flow through `pull_and_apply`.** Fetching the bare repo and resetting
  an environment's worktree to the new branch tip yields a `changed_files` list,
  which feeds the same change-classification path
  ([[0006-git-driven-change-classification]]) used for the primary repo, so an
  addon update installs/upgrades/restarts as appropriate.

## Consequences

- An environment can faithfully reproduce a **multi-repo** Odoo deployment, which
  is the common real-world shape — without each environment paying the disk and
  time cost of independent full clones.
- The **bare-clone + worktree** split makes "many environments, few repos,
  different branches" efficient and correct: object storage is shared, branch
  pinning is per environment.
- Making **branch explicit** removed a class of silent misconfigurations at the
  cost of a slightly more verbose request (`name:branch`).
- The lifecycle gained guardrails — protection markers and dependency checks —
  because shared repos are long-lived team assets, unlike disposable
  environments; deleting one out from under live environments would break them.

## Evolution

- An early fix used the **Odoo image version** (e.g. `odoo:18.0` → `18.0`) as a
  fallback branch when none was given (`ea297fd`), since `default_branch`
  (`prod`) rarely exists in addon repos. That fallback was later removed in
  favour of **requiring an explicit branch** (`9254169`) — the current behaviour.

## History

- `d495dd8` (2026-02-13) — extra addons repos support: bare clone, list, delete,
  and read-only worktree mount into environments.
- `54bc937` (2026-02-20) — configure `+refs/heads/*:refs/heads/*` fetch refspec
  on bare clones (and auto-migrate existing ones) so `git fetch` updates
  branches.
- `ea297fd` (2026-02-20) — fall back to the `odoo_image` version as the worktree
  branch when none specified (later superseded).
- `3c678cc` (2026-02-20) — add the `update_extra_repo` MCP tool.
- `1245d6c` (2026-02-20) — protect / unprotect extra repos; guard deletion with a
  `ProtectedError`.
- `9254169` (2026-02-24) — require explicit `name:branch`; remove the
  default-branch fallback and the `ODUFLOW_DEFAULT_BRANCH` setting.
- `a80ba53` (2026-02-27) — `fetch_extra_repo` returns a change summary
  (new/deleted/updated branches + commit counts); `pull_and_apply` updates
  extra-addon worktrees alongside the main repo.
