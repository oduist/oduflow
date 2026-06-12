# Product

## Register

product

## Users

Odoo developers, integrators, and dev/agency team operators who run Oduflow on
their own machine or server. The dashboard's user is the human operator:
monitoring per-branch Odoo environments, checking status and logs, starting,
stopping, syncing, and managing templates, auxiliary services, volumes, extra
addon repos, and git credentials. They are terminal-native engineers; much of
the day-to-day work is done by AI agents over MCP, and the dashboard is the
human window into what the agents and the machinery did. Typical context: a
quick operational check ("is my branch environment up? why did the install
fail?") on a desktop browser, side-by-side with an editor and terminal, almost
always in a dark-themed setup.

## Product Purpose

Oduflow provisions isolated, ephemeral Odoo environments on Docker (one per
git branch) and exposes them to AI coding agents via MCP. The web dashboard
(`src/oduflow/templates/dashboard.html`, served by `web_ui.py`) is the
operator console for that machinery: environment cards with status and
per-container stats, lifecycle actions, sync results, log viewers, web
terminal and SQL consoles, template profiles, services, volumes, extra addon
repos, credentials, agent guides, and license state. Success: the operator
understands system state at a glance and performs any lifecycle action in one
or two clicks, trusting that what they see reflects the real Docker state.

## Brand Personality

The same brand as oduflow.dev: **precise, fast, confident** — "The Engineer's
Console" carried from the marketing site into the working instrument. The
dashboard *is* the machine the site only demonstrates, so it must practice the
brand even harder: real data density, real terminal output, exact alignment,
zero marketing tone. UI copy speaks as an expert peer: verb + object button
labels, concrete nouns (template DB, filestore, container), no fluff.

## Anti-references

- **Glossy SaaS landing grammar**: gradients, gradient text, glassmorphism,
  marketing tone in UI copy.
- **Toy no-code builders**: oversized cards, cartoon illustration, emoji used
  as UI affordances.
- **Overloaded enterprise consoles** (carried from the site's PRODUCT.md):
  walls of undifferentiated buttons and tables, cluttered chrome.
- **GitHub-clone anonymity**: the current palette is GitHub's dark theme
  verbatim. The dashboard should read as Oduflow's own console (per
  DESIGN.md), not a borrowed identity.

## Design Principles

- **Show the machine.** Status, stats, logs, and sync output *are* the
  interface. Surface real values; never replace them with decorative
  summaries.
- **One style with oduflow.dev.** The dashboard uses the same token system
  ("The Engineer's Console": console field, Console Blue, signal palette,
  monospace voice) adapted to product-register density.
- **Earned density.** Dense rows, compact controls, full data — but every
  surface stays scannable. Hierarchy comes from weight and tone, not
  decoration.
- **State is sacred.** Every control reflects real state (running / partial /
  exited, protected, in use); destructive actions are explicit and guarded.
- **Self-contained.** The dashboard is served from the user's own
  infrastructure. No external CDNs, fonts, or trackers — every asset ships
  with the package.

## Accessibility & Inclusion

Pragmatic baseline, no formal WCAG certification target (internal operator
tool, by explicit decision): keep text contrast at or above 4.5:1 on the dark
surfaces (the current palette passes), keep visible focus states and keyboard
operability for the primary flows (tabs, modals, forms), and never convey
state by color alone (status badges always carry text). Provide
reduced-motion alternatives wherever animation exists.
