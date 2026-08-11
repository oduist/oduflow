# Odoo 16 Development Guide

> **Constraint:** write everything to Odoo 16.0 conventions — don't pull in patterns
> that only land in 17+ (removed `attrs`/`states`, `<list>`, `_compute_display_name`).

Quick orientation for an agent about to author or refactor a module on 16.0.

## Runtime baseline

- Python **3.10** supported (3.8+ accepted)
- PostgreSQL **12+**

## Conventions that matter on 16.0

### Models / ORM

- **`@api.model_create_multi`** is the expected `create()` signature (list of value
  dicts).
- Use the **`Command`** namespace for x2many writes
  (`Command.create(...)`, `Command.link(...)`, `Command.set(...)`).
- `name_get()` is still the supported display-label hook.

### Views & front-end

- List views still use **`<tree>`**.
- `attrs` and `states` still work — **but this is the last comfortable release for
  them**. They are removed in 17, so avoid leaning on them in new code; where it's
  cheap, prefer plain conditional attributes.
- **OWL 2** is the component framework. New front-end components should be OWL
  components, with their JS registered through the manifest `assets` key.

## Watch out for

- Don't jump ahead to later removals: keep `attrs`/`states` and the `<tree>` tag.
  Inline `invisible="..."` replaces `attrs` only from 17; `<tree>` becomes `<list>`
  only from 18.
- `_rec_names_search` **is** available on 16 — declare it (`_rec_names_search = [...]`)
  instead of overriding `name_search()`.
- Keep the manifest `version` in the full `16.0.x.y.z` form.

## Migrating a module to Odoo 16

Module upgrade scripts live in `migrations/<manifest-version>/` and run when you bump
the manifest `version`.

- The folder name must match the manifest `version` **exactly** (e.g.
  `migrations/16.0.1.1.0/`) or the scripts are silently skipped.
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
