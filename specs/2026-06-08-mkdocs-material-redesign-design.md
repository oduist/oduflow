# MkDocs Material Redesign — Design

**Date:** 2026-06-08
**Branch:** `docs-material-redesign`
**Goal:** Replace the dated `gitbook` MkDocs theme with a modern, attractive design
in the spirit of `https://docs.oduist.com/connect` (Mintlify: dark-first,
Inter + JetBrains Mono), while keeping MkDocs and the existing `.md` content.
Brand accent matches the main site `https://oduflow.dev` — **blue**, not teal.

> Spec lives outside `docs/` so MkDocs (docs_dir = `docs/`) does not publish it.

## Decisions (approved)

1. **Theme:** migrate `gitbook` → **MkDocs Material**. Content `.md` files unchanged.
2. **Mode:** **dark by default** + light toggle. Brand accent **blue `#2e79f5`**
   (from oduflow.dev), hover/accent **`#4f8dff`**, dark-mode links **`#74a8ff`**.
   Blue-tinted near-black background **`#060a12`** (matches oduflow.dev), closer to
   the reference than Material's default `slate`.
3. **Typography:** Inter (text) + JetBrains Mono (code).
4. **Homepage:** custom **hero + feature card grid** landing page.

## Scope

### A. Build / dependencies
- `requirements-docs.txt`: replace `mkdocs-gitbook` with `mkdocs-material`
  (keep `mkdocs-glightbox`, `pymdown-extensions`; `mkdocs` comes transitively).
- CI `.github/workflows/docs.yml` unchanged in shape — it already installs
  `requirements-docs.txt` and runs `mkdocs gh-deploy --force` on push to `main`.

### B. `mkdocs.yml`
- `theme.name: material`.
- `theme.palette`: two entries — default **slate** (dark) with toggle to **default**
  (light); both `primary: custom`, `accent: custom` (colors via CSS variables).
- `theme.font`: text `Inter`, code `JetBrains Mono`.
- `theme.logo` / `theme.favicon`: reuse existing brand logo (copied into `docs/assets/`)
  and existing favicon.
- `theme.features`: `navigation.instant`, `navigation.instant.progress`,
  `navigation.tracking`, `navigation.top`, `navigation.footer`, `toc.follow`,
  `search.suggest`, `search.highlight`, `content.code.copy`, `content.code.annotate`,
  `content.tabs.link`.
- `markdown_extensions`: keep current (`admonition`, `pymdownx.highlight`,
  `pymdownx.superfences`, `pymdownx.details`, `toc`), add Material staples:
  `attr_list`, `md_in_html`, `pymdownx.tabbed` (alternate style), `pymdownx.emoji`
  (Material icons), `tables`, `pymdownx.tasklist`.
- `extra_css`: keep `css/custom.css`.
- **Nav grouping** into sections (same pages, grouped for a cleaner sidebar):
  - *Getting Started*: Home, Quick Start, Installation & Configuration
  - *Core Concepts*: Use Cases & Workflows, Template Management, Environment
    Management, Auxiliary Services, Extra Addons Repositories
  - *Reference*: Web Dashboard & REST API, MCP Tools Reference, CLI Reference
  - *Operations*: Traefik Routing, Multi-Team Support, Authentication & Security,
    Running in Docker, Internals
  - *About*: Licensing, Changelog

### C. `docs/css/custom.css`
- Define Material CSS variables for both schemes:
  - `[data-md-color-scheme="slate"]` and the light scheme: `--md-primary-fg-color`,
    `--md-accent-fg-color`, and dark background/surface overrides for the near-black look.
- Keep existing `.headerlink` hover rules.
- Hero + card styles (section D), scoped to `.md-typeset`.

### D. `docs/index.md` (landing page)
- Front matter `hide: [navigation, toc]` for a landing feel.
- **Hero**: large title "Oduflow", subtitle (AI-first Odoo dev & CI), two CTA buttons
  — "Quick Start" (→ quick-start) and "GitHub" (→ repo). Built with HTML + scoped CSS
  (no `custom_dir` template override — version-resilient).
- **Card grid** (Material grid cards via `attr_list` + `md_in_html`) linking key
  sections / highlighting core features.
- Existing index content (Spec-Driven Development section, comparison table, key
  features, ASCII diagram) retained **below the hero** on the same page.

## Out of scope (YAGNI)
- No rewording of section docs.
- No converting ASCII diagrams to Mermaid.
- No `mkdocs-material` social-cards/og-image plugin (pulls cairo deps into CI).
- No `custom_dir` HTML template overrides.

## Verification
- `mkdocs serve` locally; screenshot home (dark + light), a content page, and the
  sidebar. Confirm: theme loads, palette toggle works, fonts applied, code copy button,
  hero + cards render, nav grouping correct, build emits no warnings (`mkdocs build --strict`).
