# 0030 — Odoo.sh import brings the addons-path, not just the database

**Status:** Adopted (still in force)
**Type:** Architecture / Capability
**First introduced:** `litnimax/madrid-v1` branch (2026-07-04)
**Key code today:** `templates/import-odoo.sh` (addons-path detection + `--with-*` flags + tar/remote upload), `web_ui.py` (`/api/templates/import/addon` + `/addon-remote` ingest, addon progress in `import/status`, `--with-*` composition in `import-token`), `docker_ops/system_ops.py` (`extract_addon_dir`, `_wire_imported_addons`, `rename_template`), `extra_addons.py` (`create_local_repo`, `.local` fetch short-circuit), dashboard import modal checkboxes + template Rename

## Context

The push-based Odoo.sh import ([[0023-import-from-odoo-sh]]) restores a template
from the platform's daily backup — but that backup contains only the **database
and filestore**. Odoo.sh actually runs with a much wider `--addons-path`, and a
restored database will not boot without the modules it references. A real build
line looks like:

```
--addons-path=/home/odoo/src/odoo/addons,/home/odoo/src/odoo/odoo/addons,
  /home/odoo/src/enterprise,/home/odoo/src/themes,/home/odoo/src/user,
  /home/odoo/src/user/OCA/account-reconcile,/home/odoo/src/user/OCA/bank-statement-import
```

Those paths fall into distinct classes with different reachability:

- **Standard Odoo addons** (`.../src/odoo/addons`, `.../src/odoo/odoo/addons`) —
  already ship inside the `odoo:<ver>` Docker image; nothing to fetch.
- **Enterprise** (`odoo/enterprise`) and **Themes** (`odoo/design-themes`) —
  proprietary Odoo repos over SSH with **no public access**; Oduflow cannot
  clone them. The only copy a shell can reach is the checked-out files on the
  build filesystem.
- **The customer's own repo** (`.../src/user`) — they already have it (their
  `repo_url`); not ours to fetch.
- **Extra repos** (OCA and similar, added in Odoo.sh settings) — git worktrees
  whose `origin` is usually a **reachable public** `https://` URL.

The addons live on the build filesystem, not in the backup, so they must be
inspected and transferred separately from the DB/filestore stream. Reuse of
[[0010-extra-addons-repositories]] was the obvious target — but that subsystem
assumed **every** extra repo has a remote to clone and fetch from, which is
false for Enterprise/Themes.

A separate, long-standing gap: templates could be created and deleted but never
**renamed**.

## Decision

Extend the Odoo.sh client so that, when asked, it also carries over the
addons-path as Oduflow **extra-addons** wired into the new template — reusing
[[0010-extra-addons-repositories]] end to end. Reachability decides the
mechanism, not the user: reachable repos are **cloned from their origin** (and
stay updatable); unreachable ones (Enterprise, Themes, private extras) are
**downloaded as files** into a new kind of **local, remote-less** extra-addons
repo. Selection is three checkboxes in the import dialog, gated behind a
licensing acknowledgment. Separately, add **template rename**.

The design deliberately does **not** offer to push imported addons into a
user-supplied empty repo, and does not try to preserve both the exact Odoo.sh
commit *and* updatability: a branch-based extra-addons model can give one or the
other, and updatability (a reachable remote) is the more useful of the two.

## How it works (macro)

- **Checkboxes → flags, classification at runtime.** The modal has static
  *Enterprise / Themes / Extra Addons* checkboxes plus an acknowledgment tied to
  a licensing-notes link (`oduflow.dev/odoo-sh-import-notes`). Checked boxes make
  the mint endpoint append `--with-enterprise` / `--with-themes` /
  `--with-extra-addons` to the one-line command. There is no second "analysis"
  script and no report round-trip: the client itself reads the live
  `--addons-path` (from the running `odoo-bin`, falling back to `odoo.conf`) and
  classifies each entry at run time. If it cannot find the addons-path it warns
  and still imports the DB/filestore.
- **Two ingest shapes, mirroring the filestore stream.** For a repo that will
  become local (Enterprise, Themes, a private extra), the client tars the
  directory (`--exclude=.git`) and streams it to
  `/api/templates/import/addon?name=…&branch=…`; the server extracts it
  atomically (temp dir → rename, zip-slip guarded) into
  `staging/<tpl>/addons/<name>/`, exactly like a filestore chunk. For a reachable
  extra it uploads **nothing** — it announces `{name, origin_url, branch}` to
  `/api/templates/import/addon-remote`. Both record the addon in a staged
  `addons.json`. Progress for both is disk/manifest-derived and surfaced in
  `import/status`, so a resumed run skips what already landed.
- **Finalize wires addons into the template.** After the DB restore,
  `_wire_imported_addons` turns each staged addon into an extra-addons repo:
  `kind == "remote"` is `clone_extra_repo`'d from its origin (updatable via the
  normal `update_extra_repo`), everything else is seeded by the new
  `create_local_repo`. Their `{name: branch}` is merged into the template's
  `extra_addons` metadata, so environments created from the template mount the
  same addons-path Odoo.sh ran with. One bad addon is logged, never fatal — the
  database/filestore is the critical artifact.
- **Local (remote-less) extra-addons.** `create_local_repo` builds a real bare
  git repo with a single branch seeded from the uploaded files and drops a
  `.local` marker. The whole worktree / mount / `pull_and_apply` machinery is
  unchanged because the repo has a genuine branch. The one behavioural change is
  a short-circuit at the top of `fetch_extra_repo`: a `.local` repo reports
  "up to date" instead of running `git fetch` (there is no origin). That single
  guard covers worktree creation, pulls, and the REST/MCP update path; the UI
  tags such repos "local (no remote)".
- **Template rename.** `rename_template` renames the template directory and,
  when loaded, `ALTER DATABASE … RENAME`s the PostgreSQL template DB
  (`datistemplate` toggled off/on, sessions terminated first). DB rename runs
  first (reversible) then the directory; a directory failure rolls the DB name
  back so the two never diverge. It is **refused** when any environment
  references the template — the `oduflow.template` container label is immutable
  on a running container and the overlay-remount path matches on it, so renaming
  out from under an environment would orphan it (same stance as
  `delete_extra_repo` on in-use repos). Exposed as an MCP tool, a
  `/api/templates/{name}/rename` endpoint, and a dashboard Rename button.

## Consequences

- **Imported templates boot.** A template restored from Odoo.sh can carry the
  Enterprise/Themes/extra modules its database needs, so environments spun from
  it come up with the same addons-path — no manual repo wiring afterward.
- **Reachability, not a wizard, picks the mechanism.** Public repos stay
  updatable (cloned, fetchable); proprietary/private code is vendored as a local
  copy. Bandwidth is spent only where a clone is impossible.
- **A new repo class.** Extra-addons are no longer always remote-backed. The
  `.local` marker and the fetch short-circuit are the whole cost; a local repo
  is simply never updatable from an origin, which is correct for
  Enterprise/Themes.
- **Licensing is surfaced, not enforced.** Downloading Enterprise/Themes copies
  proprietary Odoo code; the dialog links the rights/obligations notes and gates
  the download options behind an acknowledgment, but the responsibility remains
  the operator's.
- **Rename is intentionally conservative.** Blocking rename of an in-use
  template avoids container relabeling/recreation; the operator deletes the
  environments first or leaves the name. New public surface is unchanged — the
  two new ingest endpoints join the existing token-authed
  `/api/templates/import/*` family; rename stays behind the UI login.

## Evolution

- **Chunked large-payload upload (2026-07-04).** Deploying behind a Cloudflare
  Tunnel surfaced Cloudflare's 100 MB request-body cap: the ~114 MB SQL dump (and
  Enterprise/Themes tars, also >100 MB) were rejected at the edge with `413`
  before reaching Oduflow. The dump and addon uploads were therefore made
  **chunked**: the client splits the file into ≤90 MiB byte-range parts
  (`offset`/`total` query params) and the server appends them in order,
  assembling the file when the last part lands — mirroring how the filestore is
  already split by hash-directory. It is resumable (the dump resumes from the
  server's reported `dump_bytes`) and idempotent (a re-sent final part after
  completion just reports done; an out-of-order part gets a `409` with the
  expected offset). The legacy single-shot upload path is retained for callers
  that don't pass `offset`. This keeps the import working under any body-size
  cap, not just Cloudflare's. (Note: filestore hash-dir chunks are assumed
  <100 MB; a single oversized bucket would need the same treatment.)

## History

- `litnimax/madrid-v1` (2026-07-04) — addons-path import: `--with-*` flags and
  runtime classification in `import-odoo.sh`; `/api/templates/import/addon` +
  `/addon-remote` ingest and addon progress in `import/status`;
  `extract_addon_dir` + `_wire_imported_addons` in `system_ops`; local
  remote-less repos (`create_local_repo` + `.local` fetch short-circuit) in
  `extra_addons`; dashboard import checkboxes + licensing acknowledgment; and
  `rename_template` (MCP tool + REST + dashboard Rename).
