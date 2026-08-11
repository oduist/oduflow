# 0042 — Translation tooling: Odoo's own exporter as the single source of truth, plus artifact egress

**Status:** Adopted
**Type:** MCP capability
**First introduced:** this change (2026-07-27)
**Key code today:** `po_tools.py` (`parse_po`, `summarize`, `merge_with_template`, `compare`), `docker_ops/odoo_ops.py` (`export_module_translations`, `translation_status`, `_export_po`, `_export_command`, `_i18n_basename`), `server.py` (both tools, `_artifact_url`), `artifact_tokens.py`, `web_ui.py` (`/oduflow-artifact`), `scoped_access.py` (allowlist)

## Context

Translating a module is a routine part of Odoo work and Oduflow had nothing for
it: no i18n code at all, so agents assembled `odoo --i18n-export` command lines
by hand through `run_odoo_command` and wrote ad-hoc verification scripts in
`run_odoo_shell`.

That would be a minor gap if the failure modes were loud. They are not. Odoo's
`PoFileReader` derives a translation's type and target from the entry's `#:`
reference line; an entry without one is skipped, so a perfectly valid gettext
file can import as **zero translations** while the log says only "loading …".
An entry without a `#. module:` comment can make the reader call `.groups()` on
a `None` match and abort the import. Odoo avoids either outcome when a sibling
`<module>.pot` supplies the missing metadata through its automatic polib merge;
status therefore has to inspect the effective merged catalogue, not only the raw
PO. Neither real defect is visible without querying the result afterwards. A real
project shipped fully non-functional `pl.po`/`ru.po` files this way and only
found out by chance.

Two further forces shaped the design:

- **`ir.translation` was removed in Odoo 16.** Any status check written against
  it would work on 15 and silently mean nothing on 16+, where field translations
  live in jsonb columns and `_()` messages are not in the database at all.
- **Artifacts had no way out of an environment.** Oduflow could push code *in*
  (git push + `pull_and_apply`, or a live-mount) but the only way to get a
  generated file *back* was a tool response — i.e. the agent's context window. A
  32 KB `.pot` is a bad thing to pay context for when the agent only wants to
  save it to disk.

## Decision

Add two MCP tools built on **one primitive**: Odoo's own translation exporter,
run inside the environment with its real addons path.

- `export_module_translations(env_name, module, lang?)` — the `.pot` template, or
  a `.po` filled from the database when `lang` is given.
- `translation_status(env_name, module, langs?)` — the same export used as a
  measuring instrument, compared against the module's committed `i18n/*.po`.

And a general-purpose egress channel for generated files: a one-time,
short-lived download token served at `/oduflow-artifact`.

Deliberate choices:

- **Odoo's exporter, not our own.** Deriving `#:` references from the module's
  AST is possible — it is what people fall back to — but it re-implements
  behaviour that shifts between majors. The real exporter picks up `_()`/`_lt()`
  messages by walking `addons_path` and extracting from the module's Python
  sources, so it only needs the environment's own configuration to work;
  invoking `odoo` by hand with a partial addons path is the usual reason people
  conclude those terms "are not exported" (verified: exporting `base` on a live
  Odoo 19 yields 540 `code:` references). The tool prints the per-type breakdown
  so a `code: 0` result is visible rather than assumed.
- **Export-with-lang *is* the status check.** Because the exporter fills msgstr
  from wherever the running Odoo keeps translations, the same call answers "what
  is in the database" on 15 and on 18 with no version branching and no
  `ir.translation` dependency. One mechanism, not two.
- **Report, never the file.** Responses carry counts, defect flags and diffs.
  The catalogue itself goes to the module's `i18n/` directory and to a download
  URL.
- **Two tools, not four.** `import_translations` and `install_languages` were
  considered and dropped: importing is the existing upgrade path, and
  `translation_status` reporting `language active: no` covers the activation
  trap without a tool of its own.

## How it works (macro)

`export_module_translations` runs the exporter into a temp file, reads it into
the *server's* memory, summarises it, and writes it to
`<module>/i18n/<name>.pot|.po`. That directory is under `/mnt/extra-addons`, a
read-write bind mount of the environment's checkout — so in live-mount mode the
file appears directly in the developer's working tree. Shared extra-addons
checkouts are mounted read-only and are reported as such instead of failing.

Two version-shaped details are handled where they belong, alongside the existing
`--gevent-port`/`--longpolling-port` precedent:

- **Odoo 19 dropped the `--i18n-*` server options** for an `odoo i18n export`
  subcommand. That subcommand parses its argv strictly, so it rejects the
  `--db_*` arguments the official image's entrypoint appends — the route every
  other module command here takes — and it accepts no connection flags of its
  own. On 19+ the connection therefore arrives through a generated config file,
  with the password still travelling in `PGPASSWORD` rather than onto disk or
  into the container's `ps`. Everything from 15 to 18 keeps the flag form.
- **The output filename follows Odoo's `get_iso_codes` rule**, because module
  loading looks for `i18n/pl.po`, not `i18n/pl_PL.po` (while `pt_BR` stays as
  it is). Writing the raw locale would produce a file Odoo silently never reads
  — the same class of failure the tools exist to expose.

`translation_status` lines up three sources per language: the `.pot` (what the
module exposes), an export with `-l` (what the database holds), and the
committed `.po` (what is in git). If the committed catalogue has a sibling
`<module>.pot`, the status check reproduces the import-relevant part of Odoo's
polib merge: translated strings remain, current module comments/references come
from the POT, removed terms become obsolete, and new ones are untranslated. The
effective result is linted for the two silent failures, while the raw PO is
still diffed against a fresh export for missing and stale entries.

`po_tools.py` holds the reading and counting as pure functions — no Docker, no
database — so the interesting cases are ordinary unit tests.

Artifact egress mirrors [[0031-connect-as-user-impersonation]]'s token pattern
and [[0036-cross-subdomain-connect-as-landing]]'s public landing route: the tool
stashes the bytes behind a single-use token with a 10-minute TTL and returns a
URL the agent fetches with `curl -o`. The token is the sole credential, so the
path joins `_PUBLIC_PATHS` alongside `/oduflow-connect`. Port-mode URLs use the
configured team hostname rather than a listener bind address. Under stdio no web
server is mounted, so no URL is offered — Oduflow and the agent share a machine,
and the response carries the checkout path or, for read-only/core modules, a
private bounded temporary file owned by the Oduflow process.

## Consequences

- Translation work becomes a normal part of the agent loop, and the expensive
  silent failure is reported on the first call rather than discovered later.
- The command shape is now version-dependent, so a future major that moves the
  exporter again needs another branch. That is the cost of using the supported
  entry point instead of calling `trans_export` directly — whose signature also
  changed (`cr` → `env`) between 18 and 19, so neither route is stable.
- The download channel is general: any future tool that produces an artifact can
  hand it over without spending context. It is also a new unauthenticated route,
  which is why the token is single-use and short-lived, the store is bounded, and
  the artifact size is capped.
- Both tools take the per-environment lock: they start a full Odoo process, so
  they are as heavy as install/upgrade, not as cheap as a file read.
- Multi-module export is not supported. Odoo writes one file per invocation and
  its own layout is per-module; mixing modules into one catalogue would produce
  something no module could ship.

## History

- Motivated by a field report on translating an Odoo 15 module under Oduflow
  (silent `.po` import failure, no i18n tooling, no way to retrieve a generated
  file), 2026-07-27.
- Verified end to end against a live Odoo 19 environment, which is what surfaced
  both the `odoo i18n` subcommand migration and the `get_iso_codes` filename
  rule.
- Corrected status to model Odoo's sibling-POT merge and hardened catalogue I/O
  and artifact egress after review, 2026-08-11.
