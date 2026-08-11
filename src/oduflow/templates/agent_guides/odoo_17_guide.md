# Odoo 17 Development Guide

> **Constraint:** write everything to Odoo 17.0 conventions. 17 is where several
> long-running deprecations finally bite — read the "Watch out for" list before
> touching views or JS.

Quick orientation for an agent about to author or refactor a module on 17.0.

## Runtime baseline

- Python **3.10+**
- PostgreSQL **12+**

## The headline change: `attrs` / `states` are gone

The `attrs` and `states` view attributes are **removed**. Express conditions directly
on the element with a Python expression instead:

```xml
<!-- before (≤16) -->
<field name="discount" attrs="{'invisible': [('state', '!=', 'draft')]}"/>

<!-- 17.0 -->
<field name="discount" invisible="state != 'draft'"/>
```

The same applies to `readonly` and `required` — set them inline
(`readonly="state == 'done'"`, `required="is_company"`).

## Other conventions on 17.0

### Views

- The list-view root tag is still **`<tree>`** on 17. The `<tree>` → `<list>` rename
  lands in 18, so a `<list>` view here fails validation at install — keep `<tree>`.

### Models / ORM

- Replace a hand-written `name_search()` override with **`_rec_names_search`**:

  ```python
  class LibraryBook(models.Model):
      _name = "library.book"
      _rec_names_search = ["title", "isbn"]  # searched automatically
  ```

- Control the display label by overriding **`_compute_display_name()`** — on 17 the
  computation direction reversed. `name_get()` is now only a deprecated shim that
  returns `display_name`; the core no longer calls it, so overriding `name_get()` has
  **no effect** on the record's displayed name.
- Keep using **`@api.model_create_multi`** and the **`Command`** namespace for x2many.

### JavaScript / OWL

- OWL 2 is the only component model. Custom field widgets extend **`Component` from
  `@odoo/owl`** — the legacy widget registry and **`AbstractField` are gone**.
- Import OWL from the module, never the global:

  ```javascript
  import { Component, useState, onMounted } from "@odoo/owl";
  ```

- Port OWL 1 lifecycle methods to hooks called inside `setup()`:
  `mounted()` → `onMounted(...)`, `willUnmount()` → `onWillUnmount(...)`,
  `willStart()` → `onWillStart(...)`, `willUpdateProps()` → `onWillUpdateProps(...)`.

## Watch out for

- No `attrs`/`states` anywhere — they now raise at view validation.
- No `owl` global, no `AbstractField`.
- Keep the manifest `version` in the full `17.0.x.y.z` form.

## Migrating a module to Odoo 17

Module upgrade scripts live in `migrations/<manifest-version>/` and run when you bump
the manifest `version`.

- The folder name must match the manifest `version` **exactly** (e.g.
  `migrations/17.0.1.1.0/`) or the scripts are silently skipped.
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
    if not version:  # fresh install — nothing to port
        return
    openupgrade.rename_fields(
        env,
        [
            ("library.book", "library_book", "isbn_code", "isbn"),
        ],
    )
```
