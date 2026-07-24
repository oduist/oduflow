# 0021 — Code delivery modes: `repo_url` (git push) vs `local_path` live-mount, with an explicit `pull_and_apply` guardrail

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `aca9445` "Stdio live-mount + explicit pull_and_apply guardrail" (2026-06-12)
**Key code today:** `server.py` (`create_environment` `local_path`/`repo_url`, `pull_and_apply`, `get_agent_instructions`), `settings.py` (`TRANSPORT`, `allow_local_path`), `docker_ops/env_ops.py` (bind-mount vs clone, `pull_environment`), `git_analysis.py` (guardrail)

## Context

There are two very different ways an agent works, and the original design assumed
only one. In the founding model
([[0001-mcp-orchestrated-ephemeral-per-branch-environments]]) the agent develops
by **pushing code to a branch** and asking the environment to pull and apply it
([[0006-git-driven-change-classification]]). That is exactly right for a
**remote, multi-user** deployment ([[0002-remote-multi-user-mcp-access]]): the
server runs elsewhere, code can only reach it over git, and pushing is the audit
trail.

But for a developer running Oduflow **locally**, the git round-trip is pure
friction. The agent already has the checkout open on the same host as the Docker
daemon; making it commit and push to GitHub just so Oduflow can pull the change
back onto the same disk is a slow, noisy dev loop for what is conceptually "edit
a file and reload". Local operators may use stdio or run the HTTP transport to
get the dashboard; code delivery should behave the same in both.

A second force: when the agent *authored* the edits, it already knows what they
are. Forcing every change through pure auto-classification is both unnecessary
(the agent can just say "upgrade this module") and risky (auto-detection is
heuristic). Yet leaving the agent fully in charge invites the classic mistake of
"I only restarted, but I changed a field" — stale schema, ghost-chasing.

## Decision

Support **two code-delivery modes**, chosen per environment, and make applying
changes an **explicit, classified step** in both.

- **`repo_url` (git mode) — default.** The agent
  pushes to its branch; `create_environment(repo_url=...)` clones it; later
  `pull_and_apply` pulls the branch into the managed clone and applies the right
  Odoo action. This is the multi-user CI path and stays the default.
- **`local_path` (live-mount) — trusted local fast-path.**
  `create_environment(local_path=<abs path>)` **bind-mounts the agent's own
  checkout** straight into the container instead of cloning. Edits are visible
  inside Odoo instantly; there is no push, no pull, no second copy on disk. It is
  gated by `allow_local_path` and works through both stdio and HTTP so a local
  developer can use the dashboard. Because the mount is read/write and names a
  host path, it is a trusted single-user feature; hosted, remote, and multi-user
  operators disable it.
- **`pull_and_apply` as an explicit guardrail, not implicit auto-sync.** Applying
  is always a deliberate tool call, never a hidden file-watcher. The tool is
  transport-agnostic and parameterised: the agent passes explicit
  `install` / `upgrade` / `restart` because it authored the edits. A guardrail
  reuses change classification ([[0006-git-driven-change-classification]]) to
  cross-check the *requested* action against the *detected* diff and appends
  **non-blocking warnings** when an action looks missing (`strict=True` refuses
  instead). Leaving the args empty falls back to full auto-classification —
  useful when pulling commits the agent did not author.
- **The active mode is advertised to the agent.** `get_agent_instructions`
  inspects the environments and emits a "Current Code Delivery Mode" preface, so
  the agent uses the live-mount workflow (edit + apply, git optional) or the
  git workflow (commit + push + apply) appropriately, without guessing.

## How it works (macro)

- **Mode picked at create time, recorded on the environment.** Passing
  `local_path` selects live-mount; passing `repo_url` (or a template that carries
  one) selects git mode. In live-mount mode the "pull" inside `pull_and_apply` is
  a no-op — the files are already live — while git mode still fetches and resets
  the managed clone. The same tool, the same apply semantics, regardless of mode.
- **Change detection adapts to the source.** Git mode diffs commits; live-mount
  mode reads the checkout's own working tree, with an mtime-snapshot fallback for
  non-git directories. Either way the guardrail and the classifier see a
  `changed_files` set and recommend the minimal correct action.
- **Safety boundary on the live-mount.** Live-mount is the fast path *because* it
  trusts the caller with host-filesystem access. `allow_local_path` is enabled
  for the local single-user workflow and must be disabled for hosted, remote, or
  multi-user deployments; startup emits a security warning while it is enabled.

## Consequences

- The dev loop collapses for local work: **edit → `pull_and_apply` → see it**,
  with no GitHub round-trip — while remote multi-user CI keeps the push-based,
  auditable git path. One system serves both audiences without compromising
  either.
- Making apply an **explicit guardrailed step** (rather than auto-syncing on
  file change) keeps the agent in control and the action correct: it states what
  it changed, and the system catches under-actions before they cause stale-schema
  confusion. It also keeps recommendation logic in one place, shared with the git
  path.
- The two modes share almost all machinery (`pull_and_apply`, classification,
  the environment lifecycle); the difference is localized to *clone vs
  bind-mount*, keeping the surface small.
- Templates had to learn about the mode too: a template saved from a
  live-mounted environment carries a `local_path` instead of a `repo_url` and
  recreates the live-mount on use (when `allow_local_path` is enabled).

## History

- `044ccb9` (2026-02-14) — rename `pull_environment_repository` → `sync_environment`
  (the apply entry point that `pull_and_apply` later generalised).
- `aca9445` (2026-06-12) — stdio **live-mount** (`local_path` bind-mount, gated to
  stdio) + **explicit `pull_and_apply`** with a classification-backed guardrail
  (warn by default, `strict=True` to block); mode-aware `create_environment`
  errors; apply rules documented in the agent instructions (#36).
