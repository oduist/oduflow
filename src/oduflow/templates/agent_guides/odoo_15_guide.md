# Odoo 15 Development Guide

> **Constraint:** write everything to Odoo 15.0 conventions — don't mix in patterns
> from newer releases (`<list>`, `_compute_display_name`, removed `attrs`, etc.).

Quick orientation for an agent about to author or refactor a module on 15.0.

## Runtime baseline

- Python **3.8+**
- PostgreSQL **12+**

## Conventions that matter on 15.0

### Assets

- This is the release where front-end assets moved into the **manifest `assets`
  key**. Declare bundles there instead of inheriting `<template>` XML bundles.

  ```python
  # __manifest__.py
  "assets": {
      "web.assets_backend": [
          "library/static/src/js/book_widget.js",
          "library/static/src/scss/book.scss",
      ],
  },
  ```

### Models / ORM

- Prefer **`@api.model_create_multi`** for `create()` overrides (it takes a list of
  value dicts) — it is the forward-compatible signature.
- Write x2many relations with the **`Command`** namespace rather than raw magic
  tuples:

  ```python
  from odoo import Command

  self.author_ids = [Command.create({"name": "New Author"})]
  ```

- `name_get()` is still the standard way to control a record's display label.

### Views & JS

- List views still use the **`<tree>`** tag.
- The `attrs` and `states` view attributes are still valid and idiomatic.
- OWL is being introduced but the legacy widget system is still present — follow
  whatever style the module already uses.

## Watch out for

- Do **not** retro-fit 17+/18+ idioms here: no `<list>`, no `_rec_names_search`, no
  `_compute_display_name`, keep `attrs`/`states`.
- Keep the manifest `version` in the full `15.0.x.y.z` form.
