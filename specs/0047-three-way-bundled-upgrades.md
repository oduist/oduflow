# 0047 — Three-way upgrades for deployed bundled files

**Status:** Adopted
**Type:** Architecture — configuration delivery model
**First introduced:** this change (2026-08-14), branch `litnimax/merge-local-upgrades`
**Key code today:** `bundled_upgrade.py` (baseline state machine, merge, atomic writes), `server.py` (`_ensure_initialized`, `oduflow upgrade` CLI)

## Context

Auto-init seeds a per-team `odoo.conf`, sanitization script, and agent guides
from package templates ([[0018-onboarding-stdio-default-auto-init]]). Operators
are expected to customize those deployed copies. The original upgrade command
could not distinguish an operator edit from a new package version: it compared
only file sizes and copied the new bundle over every different-size file. An
edit of the same size was missed entirely, while a detected edit was lost
unless the operator had added the all-or-nothing `# KEEP` marker.

Merging requires three versions: the operator's live file, the new bundle, and
the previous pristine bundle they share as an ancestor. Existing installations
do not have that ancestor, so their first reconciliation cannot be automated
without guessing which release supplied the file. A conflict must also never
put `<<<<<<<` markers into a live `odoo.conf` or agent instruction file.

## Decision

Treat deployed bundled files as three-way-managed configuration. Each team
stores the last accepted pristine bundle under
`.bundled_upgrade/baselines/`, mirroring the deployed relative paths. Auto-init
creates the live file and baseline together; it may adopt an existing file only
when its bytes exactly match the current bundle.

`oduflow upgrade` compares complete content and classifies each file before
writing:

- untouched local file: install the new bundle;
- local-only change: leave it untouched;
- both sides changed: merge with `git merge-file` using the stored baseline;
- first-line `# KEEP`: skip unconditionally.

Before replacing a live file, keep its previous bytes under
`.bundled_upgrade/backups/`. Live writes and state writes use temporary files
plus atomic replacement. `--force` controls only interactive confirmation; it
does not weaken preservation or conflict handling.

PostgreSQL configuration is not part of this mechanism. It is generated from
host resources and changes through the explicit preview/apply boundary in
`retune-postgres` ([[0044-unified-host-resource-planning]]).

## How it works (macro)

A clean diff3 result atomically replaces the live file and advances the
baseline to the new pristine bundle. A conflicting result is written beside
the live file as `*.oduflow-merge`; the live file and accepted baseline remain
unchanged. The candidate new baseline is held separately under
`.bundled_upgrade/pending/`. The operator resolves and installs the sidecar,
then removes it; that removal acknowledges the pending bundle as the new merge
base on the next run. Until resolution, the CLI exits non-zero and refreshes
the conflict against the accepted baseline.

For a customized pre-baseline installation, Oduflow cannot reconstruct the
ancestor safely. It therefore leaves the live file byte-for-byte intact, writes
the current bundle as `*.oduflow-new`, and records it as pending rather than
accepted. The operator performs this one-time manual reconciliation and removes
the sidecar. From then on, later package changes have a real ancestor and merge
automatically.

If the merge engine is unavailable or fails, the live file and baseline remain
unchanged and the new bundle is exposed as `*.oduflow-error-new`. Planning also
records the bytes it inspected; apply refuses to proceed if the source, live
file, or merge base changed while the confirmation prompt was open.

## Consequences

- Ordinary local customization no longer blocks upstream improvements or gets
  overwritten by them; disjoint changes merge automatically.
- Conflicts are explicit, script-detectable, and safe for parsers and agents
  because markers never enter the live file.
- Existing customized installations require one honest manual reconciliation;
  the system does not infer an unknowable historical base from file similarity
  or package tags.
- Per-team baseline, pending, and rolling backup copies consume a small amount
  of quota-accounted storage and become part of the team's persistent state.
- Git remains the merge implementation, consistent with Oduflow's Git-driven
  development model; its absence degrades to a non-destructive error artifact.

## History

- 2026-08-14 — persistent baselines, safe legacy migration, diff3 merge,
  conflict sidecars, atomic backups, and PostgreSQL exclusion introduced on
  `litnimax/merge-local-upgrades`.
