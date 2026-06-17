# 0006 — Git-driven change classification → automatic install/upgrade/restart

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `b1c97d0` "Detect module changes fix" (2026-02-08), `3db7cc1` "Detect field changes in .py files to trigger module upgrade" (2026-02-08)
**Key code today:** `git_analysis.py` (`classify_changes`, `shallow_classify`, `recommend`, `guardrail_warnings`), `git_ops.py` (`pull_repo` → `old_head`), `docker_ops/env_ops.py` (sync/apply orchestration)

## Context

An agent develops by **pushing code to a branch**, then asking the environment
to apply it ([[0001-mcp-orchestrated-ephemeral-per-branch-environments]]). But
"apply" in Odoo is not one thing: a Python method edit only needs the worker
**restarted**; a new or changed field needs the module **upgraded** (`-u`) so
the DB schema migrates; a brand-new module needs **installing** (`-i`); a pure
QWeb/JS tweak needs nothing but a hot reload.

Forces at play:
- Doing the **maximal** action every time (always `-u` everything) is slow and,
  for a new module, fails outright (`-u` on a module with no DB schema).
- Doing the **minimal** action (always just restart) silently skips schema
  migrations, so the agent sees stale behaviour and chases ghosts.
- The agent shouldn't have to reason about Odoo's load semantics on every push —
  the system should infer the right, minimal action from *what actually changed*.

## Decision

Add a **change-classification subsystem** that diffs the files changed between
two git commits and maps them to the single correct Odoo action —
`install`, `upgrade`, `restart`, `refresh`, or `none` — then drives the
environment accordingly. The classification reads **content**, not just paths,
so it can tell a schema change from a cosmetic one.

The rules, refined incrementally from real misfires:

- **Manifest changes** decide install vs upgrade: a `__manifest__.py` with no
  prior version in git is a **new module → install**; a version bump or a change
  to a file-list key (`data`, `demo`, `assets`, `qweb`) is an **upgrade**.
- **Python field changes** force an upgrade: compare `fields.*` definition lines
  between old and new source; if any field is added/removed/modified the schema
  may need migrating → **upgrade**. Other `.py` edits → **restart** (reload code,
  no schema work).
- **XML** in `security/` or `data/` directories → **upgrade** (it loads into the
  DB). Other XML is normally a hot **refresh**, *except* when a view's
  `<tree>`/`<list>`/`<form>` opening-tag attributes change, which also needs an
  **upgrade**.
- **JS/other view XML** → hot **refresh** (no restart, no DB work).

## How it works (macro)

- **Pre-pull commit as baseline.** Before applying, the sync step records the
  environment's current `HEAD` (`pull_repo` returns `old_head`), pulls, then
  classifies the full range `old_head..new` against that **pre-pull baseline** —
  not `HEAD~1` — so multi-commit pushes are analysed in full.
- **Content diffs via git.** `classify_changes` resolves each file to its Odoo
  module (walking up to the nearest `__manifest__.py`) and, for manifests/fields/
  view tags, compares the new working-tree content against the `base_ref` content
  via `git show`. The result is the minimal action plus the exact module lists.
- **Degraded mode for non-git mounts.** When no `base_ref` is available (a
  live-mount with no commit to diff), `shallow_classify` does a coarser
  path-only pass: it can't distinguish a field edit from a method edit (→ restart)
  or a new module from a changed one (→ upgrade). `recommend()` picks the deep or
  shallow path automatically.
- **Guardrails, not gates.** The agent may request its own action; the system
  computes the recommendation and emits **non-blocking warnings** only when the
  request looks like an *under*-action (a missing install/upgrade/restart),
  leaving the agent in charge.
- **`# KEEP` protection.** A file whose first line is `# KEEP` is never
  overwritten by `oduflow upgrade`; it is reported as `(kept)` instead. This lets
  generated/hand-tuned files (e.g. a locally edited `odoo.conf`,
  `postgresql.conf`) survive a sync.
- **Trace logging.** `ODUFLOW_TRACE=1` emits a per-file decision trace through
  the classify/sync pipeline, for debugging why an action was (or wasn't) chosen.

## Consequences

- An agent's push is applied **correctly and minimally**: schema changes migrate,
  code changes restart, cosmetic changes hot-reload, and new modules install —
  without the agent encoding Odoo's load model into every request.
- The rules are deliberately **heuristic and content-aware**; each refinement
  (fields, data/security XML, view-tag attributes) closed a concrete class of
  "I changed X but the environment didn't pick it up" bug. They will keep
  accreting as new edge cases surface.
- Tying classification to the **pre-pull commit** made the result correct for
  batched pushes and is now a load-bearing contract of `pull_repo`.
- The same subsystem feeds both the automatic apply path and the agent-facing
  guardrail warnings, keeping recommendation logic in one place.

## History

- `b1c97d0` (2026-02-08) — detect module changes to trigger an upgrade.
- `3db7cc1` (2026-02-08) — detect `fields.*` changes in `.py` files → upgrade;
  resolve the real module by walking up to `__manifest__.py`; restart after
  upgrade to load new Python.
- `f03b8f6` (2026-02-08) — distinguish **install (`-i`)** from **upgrade (`-u`)**;
  track install/upgrade module sets separately so new modules don't fail on `-u`.
- `1280c23` (2026-02-14) — XML under `data/` triggers an upgrade.
- `044ccb9` (2026-02-14) — rename `pull_environment_repository` → `sync_environment`.
- `af7b9bc` (2026-02-17) — `ODUFLOW_TRACE=1` trace logging for the sync/classify
  pipeline.
- `f69bc2f` (2026-03-05) — include `odoo.conf` in upgrade; skip files unchanged
  from the bundled version.
- `cdc9ea9` (2026-03-10) — use the **pre-pull commit** as the baseline for
  manifest/field comparison so multi-commit pushes classify correctly (#8).
- `a834ec9` (2026-03-11) — detect `<tree>`/`<list>`/`<form>` view-tag attribute
  changes → upgrade instead of hot-reload.
- `a97e13f` (2026-03-17) — `# KEEP` marker protects files from upgrade overwrite.
