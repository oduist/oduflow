# 0043 — Template code provenance and the environment lineage check

**Status:** Adopted (current)
**Type:** Architecture
**First introduced:** 2026-08-08, from a field report of repeated "the branch was cut before the template snapshot" upgrade failures
**Key code today:** `docker_ops/system_ops.py` (`_code_provenance`, template metadata, `list_templates`), `git_analysis.py` (`template_lineage`), `git_ops.py` (`commit_exists`, `is_ancestor`, `diff_names`), `docker_ops/env_ops.py` (`_template_code_lineage`, `create_environment`)

## Context

An environment is a **new database cloned from a template** plus **the branch's
own code** ([[0003-database-templates-and-filestore-isolation]],
[[0001-mcp-orchestrated-ephemeral-per-branch-environments]]). Those are two
snapshots of the same lineage, taken at different times — and nothing tied them
together. The template recorded *where the code came from* (`repo_url`,
`odoo_image`, extra addons) but never *which commit its data was snapshotted
at*.

In practice that gap produced two failures that look unrelated but are the same
problem mirrored:

- **The branch is behind the snapshot.** A branch cut from an older commit gets a
  database already carrying views and records written by newer code. The first
  `-u` validates old code against new data and dies — typically a `ParseError` on
  a view referring to a method the branch does not have. The remedy is to merge
  the template's source branch first.
- **The branch is ahead of the snapshot.** The database predates the code, so
  fields and XML IDs the code assumes are missing: `column ... does not exist`,
  `External ID not found`. The remedy is an explicit `-u` naming every affected
  module — including dependency modules whose manifest version was never bumped,
  which nothing upgrades automatically.

Agents were rediscovering both by trial and error, and reaching for the one
"fix" that cannot work: deleting and recreating the environment. The template is
unchanged, so the drift returns — minus the environment's data.
[[0006-git-driven-change-classification]] already knew how to turn a commit range
into the right module list; it simply had no range to look at, because the
template's end of the comparison did not exist.

## Decision

Record **code provenance** on every template whose data comes from one of our own
environments — the source branch, the commit its database was snapshotted at, and
when — and **compare it against the checkout at environment creation**, reporting
the drift and its remedy while the agent is still deciding what its first
`pull_and_apply` should do.

The comparison is **advisory, never a gate**. Creation proceeds regardless; a
template with no recorded commit (imported from a foreign Odoo, or created before
this record) simply produces no verdict.

## How it works (macro)

- **Provenance at snapshot time.** `save_as_template` reads the source
  environment's branch label and resolves its checkout's `HEAD`, storing
  `source_branch` / `source_commit` / `snapshot_at` in the template metadata
  alongside the existing code-source fields. Live-mounted environments use their
  mounted checkout. Imports from a running Odoo record only `snapshot_at`: there
  is no commit of ours behind that data, and inventing one would be worse than
  saying nothing.
- **A three-way verdict at creation.** After the clone (or live-mount),
  `template_lineage` compares the recorded commit with the checkout's `HEAD`:
  *aligned*, *ahead* (the snapshot is an ancestor of `HEAD`), *diverged* (it is
  not), or *unknown* (no commit recorded, or the commit is absent from this
  repository — a deleted branch, a different remote, or an unavailable fetch).
  Managed depth-one development clones fetch the current and template-source
  branch histories before this comparison; live-mounted checkouts are never
  fetched or otherwise mutated by the check.
- **The remedy comes with the verdict.** For *diverged* the message names the
  branch to merge. For *ahead*, the snapshot commit is fed as the base ref into
  the existing change classifier, so the environment reports the concrete
  `install="new_module"` and `upgrade="a,b,c"` sets rather than a generic
  warning — reusing [[0006-git-driven-change-classification]] instead of
  duplicating its rules.
- **Visible before creation too.** `list_templates` shows each template's
  `Source=<branch> @ <commit> @ snapshot <date>`, so the drift can be anticipated
  when choosing a template.
- **Failure is silence.** Any error inside the check (git missing, unreadable
  metadata, an odd repository state) degrades to *unknown*. An advisory check
  must never be able to fail a provisioning that otherwise succeeded.

## Consequences

- The rule an experienced operator carried in their head — *"merge `prod` before
  the first apply"* — is now stated by the system, in both directions, at the
  moment it matters, and is written down as a general invariant rather than a
  project-specific ritual.
- The `-i` / `-u` sets for an ahead branch are computed, not guessed, and the
  upgrade set includes dependency modules that no version bump would have
  caught.
- Templates predating provenance keep working and stay quiet; provenance accrues
  as they are re-baselined. There is no migration and no new configuration.
- The check is one more consumer of the classifier's commit-range contract,
  which further entrenches `base_ref`-style comparison as the way Oduflow reasons
  about "what changed".
- Because it only reports, a wrong verdict costs an ignorable line of text — the
  deliberate trade for running it on every creation without review.

## History

- 2026-08-08 — provenance recorded at `save_as_template` / import, surfaced in
  `list_templates`, and compared at `create_environment`; shipped together with
  the lock-holder diagnostics and `run_odoo_tests(test_tags=…)` from the same
  field report.
