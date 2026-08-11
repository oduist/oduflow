# Odoo 19 Development Guide

> **Constraint:** write everything to Odoo 19.0 conventions. 19 turns several 18-era
> "recommended" patterns into the *only* accepted ones, and rides on Python 3.12 —
> check both the runtime and ORM sections before writing code.

Quick orientation for an agent about to author or refactor a module on 19.0.

## Runtime baseline

- Python **3.10–3.14** (minimum is 3.10 — same as 18)
- PostgreSQL **14+** (15+ recommended)

### If you run on Python 3.12+

19 supports (but does not require) recent Python, so these only bite when the actual
runtime is 3.12 or newer:

- `datetime.utcnow()` is deprecated. Use `fields.Datetime.now()` for ORM values, or
  `datetime.now(timezone.utc).replace(tzinfo=None)` if you need stdlib.
- Recent Python removed several stdlib modules, which then cause **hard import errors**
  at load — grep the module for them before porting: `distutils` (gone in 3.12); `cgi`,
  `cgitb`, `telnetlib`, `pipes`, `aifc`, `sunau`, `audioop` (gone in 3.13).

## Models / ORM

- **`@api.model_create_multi` is the only accepted `create()` override.** A
  single-dict `@api.model def create(self, vals)` is deprecated and warns.

- **`name_get()` no longer exists** (removed back in 18) — use
  **`_compute_display_name()`**. A leftover `name_get()` override is silently ignored,
  and *calling* `name_get()` raises `AttributeError`. The `@api.depends` is mandatory;
  without it the label never refreshes:

  ```python
  @api.depends("isbn", "title")
  def _compute_display_name(self):
      for rec in self:
          rec.display_name = f"[{rec.isbn}] {rec.title}"
  ```

- **Do not declare a `display_name` field.** It is a built-in computed field on
  `models.Model`; re-declaring it conflicts with the ORM (and a previously stored
  column may need dropping in a migration).

## Views

- **`<tree>` is fully removed** — only `<list>` is accepted.
- Action `view_mode` uses `list` (not `tree`): `<field name="view_mode">list,form</field>`.
- In inherited views, update XPath targets too: `//tree` → `//list`, or the inherit
  fails at install.

## JavaScript / OWL

- OWL 2 only. Import from `@odoo/owl` and use lifecycle hooks inside `setup()`:

  ```javascript
  import { Component, useState, onMounted } from "@odoo/owl";
  ```

## Misc

- Domains are always **lists**, never strings: `[("state", "=", "draft")]`.

## Removed / replaced — quick map

| Old | Use instead |
| --- | --- |
| `name_get()` (removed in 18) | `_compute_display_name()` (+ `@api.depends`) |
| explicit `display_name` field | built-in computed field — remove it |
| `@api.model def create(self, vals)` | `@api.model_create_multi def create(self, vals_list)` |
| `<tree>` / `//tree` xpath | `<list>` / `//list` |
| string domains | list domains |
| `datetime.utcnow()` (on Python 3.12+) | `fields.Datetime.now()` |

## Migrating a module to Odoo 19

Module upgrade scripts live in `migrations/<manifest-version>/` and run when you bump
the manifest `version`.

- The folder name must match the manifest `version` **exactly** (e.g.
  `migrations/19.0.1.1.0/`) or the scripts are silently skipped.
- `pre-migration.py` runs *before* the ORM loads the new schema — use **raw SQL**
  (rename columns, backfill data ahead of a NOT NULL or type change).
- `post-migration.py` runs *after* the schema is in place — rename XML IDs, reshape
  data through the ORM, drop obsolete records (e.g. dropping a stored `display_name`
  column left over from 18).
- With `openupgradelib`, reach for `rename_fields`, `rename_xmlids`, `logged_query`,
  and `delete_records_safely_by_xml_id`.
- Always early-return on a fresh install (`version` is `None`), and never commit the
  cursor yourself — Odoo owns the transaction.

```python
from openupgradelib import openupgrade


@openupgrade.migrate()
def migrate(env, version):
    if not version:  # fresh install — nothing to port
        return
    openupgrade.rename_fields(
        env,
        [
            ("library.book", "library_book", "isbn_code", "isbn"),
        ],
    )
```
