# Odoo 19 Development Guide

> **Constraint:** write everything to Odoo 19.0 conventions. 19 turns several 18-era
> "recommended" patterns into the *only* accepted ones, and rides on Python 3.12 —
> check both the runtime and ORM sections before writing code.

Quick orientation for an agent about to author or refactor a module on 19.0.

## Runtime baseline

- Python **3.12+** (this is a hard jump from 18)
- PostgreSQL **14+** (15+ recommended)
- Node **18+** for asset builds

### Python 3.12 fallout

- `datetime.utcnow()` is deprecated. Use `fields.Datetime.now()` for ORM values, or
  `datetime.now(timezone.utc).replace(tzinfo=None)` if you need stdlib.
- Several stdlib modules were removed and cause **hard import errors** at load — grep
  the module for them before porting: `distutils`, `cgi`, `imghdr`, `cgitb`,
  `telnetlib`, `pipes`, `aifc`, `sunau`, `audioop`.

## Models / ORM

- **`@api.model_create_multi` is the only accepted `create()` override.** A
  single-dict `@api.model def create(self, vals)` is deprecated and warns.

- **`name_get()` is deprecated → `_compute_display_name()`.** The `@api.depends` is
  mandatory; without it the label never refreshes:

  ```python
  @api.depends("isbn", "title")
  def _compute_display_name(self):
      for rec in self:
          rec.display_name = f"[{rec.isbn}] {rec.title}"
  ```

- **Do not declare a `display_name` field.** It is a built-in computed field on
  `models.Model`; re-declaring it conflicts with the ORM (and a previously stored
  column may need dropping in a migration).

- **`read_group()` → `_read_group()`.** Arguments after the domain are keyword-only and
  the return shape changed from dicts to tuples/recordsets — rewrite the *callers*, not
  just the name:

  ```python
  for partner, total in self.env["sale.order"]._read_group(
      domain=[("state", "=", "sale")],
      groupby=["partner_id"],
      aggregates=["amount_total:sum"],
  ):
      # partner is a recordset, total is a float
      ...
  ```

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
| `name_get()` override | `_compute_display_name()` (+ `@api.depends`) |
| explicit `display_name` field | built-in computed field — remove it |
| `@api.model def create(self, vals)` | `@api.model_create_multi def create(self, vals_list)` |
| `read_group()` (dict results) | `_read_group()` (tuple results, keyword args) |
| `<tree>` / `//tree` xpath | `<list>` / `//list` |
| string domains | list domains |
| `datetime.utcnow()` | `fields.Datetime.now()` |

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
    if not version:        # fresh install — nothing to port
        return
    openupgrade.rename_fields(env, [
        ("library.book", "library_book", "isbn_code", "isbn"),
    ])
```
