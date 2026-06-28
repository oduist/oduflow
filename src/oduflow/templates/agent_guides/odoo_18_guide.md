# Odoo 18 Development Guide

> **Constraint:** write everything to Odoo 18.0 conventions. Several ORM and view
> idioms changed shape here; the cheat sheet below is what to apply by default.

Quick orientation for an agent about to author or refactor a module on 18.0.

## Runtime baseline

- Python **3.10+**
- PostgreSQL **14+**

## Models / ORM

- **Search by name without an override.** Declare the searchable fields instead of
  writing `name_search()`:

  ```python
  class LibraryBook(models.Model):
      _name = "library.book"
      _rec_names_search = ["title", "isbn"]
  ```

- **`selection_add` now requires `ondelete`.** Say what happens to existing rows when
  the extending module is removed:

  ```python
  status = fields.Selection(
      selection_add=[("archived", "Archived")],
      ondelete={"archived": "set default"},   # or "cascade", "set null", a callable
  )
  ```

- **Customize duplication via `copy_data()`, not `copy()`.** It returns a *list* of
  value dicts:

  ```python
  def copy_data(self, default=None):
      vals_list = super().copy_data(default=default)
      for vals in vals_list:
          vals["title"] = _("%s (copy)") % vals.get("title", "")
      return vals_list
  ```

- **`create()` takes a list.** `@api.model_create_multi` with `vals_list` is the
  standard form:

  ```python
  # before
  @api.model
  def create(self, vals):
      return super().create(vals)

  # 18.0
  @api.model_create_multi
  def create(self, vals_list):
      for vals in vals_list:
          vals.setdefault("state", "draft")
      return super().create(vals_list)
  ```

- **Future-proof the display label.** `_compute_display_name()` already works on 18,
  so prefer it over `name_get()` to ease the move to 19.

## Views

- Use **`<list>`** everywhere `<tree>` used to appear — including inside one2many
  `<field>` blocks:

  ```xml
  <field name="line_ids">
      <list editable="bottom">
          <field name="product_id"/>
      </list>
  </field>
  ```

## Assets & misc

- Backend bundle is **`web.assets_backend`**, public/website bundle is
  **`web.assets_frontend`**.
- JS is OWL 2 via `@odoo/owl` (unchanged since 17).
- Drop **`view_type`** from `ir.actions.act_window` — only `view_mode` matters.
- Import the translation helper from the package root: `from odoo import _`
  (never `from odoo.tools.translate import _`).
- For company-scoped relations, set `_check_company_auto = True` on the model and
  `check_company=True` on the relevant Many2one.

## Watch out for

- No `<tree>`, no `view_type`, no `selection_add` without `ondelete`.
- Overriding `copy()` to tweak duplicated values no longer fits — use `copy_data()`.

## Migrating a module to Odoo 18

Module upgrade scripts live in `migrations/<manifest-version>/` and run when you bump
the manifest `version`.

- The folder name must match the manifest `version` **exactly** (e.g.
  `migrations/18.0.1.1.0/`) or the scripts are silently skipped.
- `pre-migration.py` runs *before* the ORM loads the new schema — use **raw SQL**
  (rename columns, backfill data ahead of a NOT NULL or type change).
- `post-migration.py` runs *after* the schema is in place — rename XML IDs, reshape
  data through the ORM, drop obsolete records.
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
