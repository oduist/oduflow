---
target: src/oduflow/templates/dashboard.html
total_score: 33
p0_count: 0
p1_count: 0
timestamp: 2026-06-12T15-25-24Z
slug: src-oduflow-templates-dashboard-html
---
# Design Critique (re-run after fixes): Oduflow Dashboard

Scope reframed per owner: the dashboard is visualization-first; agents create everything via MCP. Creation ergonomics, bulk ops, shortcuts, deep links = explicit non-goals (PRODUCT.md updated).

| # | Heuristic | Was | Now | Change |
|---|-----------|-----|-----|--------|
| 1 | Visibility of system status | 2 | 4 | Busy chip (spinner + verb + elapsed) for all manual lifecycle ops, survives re-render, fixes double-fire; red [exited] tokens; PARTIAL count gloss |
| 2 | Match real world | 3 | 3 | Verb tooltips explain Update/Recreate/Sync at point of choice; the word "Update" still overloaded across entities |
| 3 | User control | 3 | 3 | unchanged (no cancel for in-flight ops) |
| 4 | Consistency | 3 | 3 | unused volume badge now neutral; repo Protect toggle still diverges from env menu item |
| 5 | Error prevention | 3 | 4 | danger confirms focus Cancel; esc() escapes quotes (attribute-injection via branch names closed) |
| 6 | Recognition vs recall | 3 | 4 | tooltips on all lifecycle verbs, template tags, PARTIAL, protect |
| 7 | Flexibility/efficiency | 2 | 3 | container selector in logs (+?container= API), copy logs, filter — right accelerators for the visualization use case |
| 8 | Aesthetic/minimalist | 3 | 3 | four-hue action rows remain; "Auto-refresh" label fixed |
| 9 | Error recovery | 2 | 4 | errors persist until dismissed + Copy; full text readable |
| 10 | Help & documentation | 1 | 2 | contextual tooltips everywhere; still no docs link |
| **Total** | | **25** | **33/40** | **Good** |

All three P1s closed within the reframed scope: long-op feedback (busy chips), PARTIAL diagnosis (container log selector + red tokens + gloss), transient errors (persistent + copyable). P2 license badge -> real button; duplicate-class bug fixed; type micro-cluster consolidated (detector confirms 10.5px step gone); h1->h3 skip fixed; relative dates with full date in title.

Detector re-run: 3 findings, all intentional-by-design (Geist Mono = brand mandate; em-dashes = title separators; flat-hierarchy now 11/11.5/12 where 11px is uppercase badge labels only).

Remaining (P3): docs link somewhere discoverable; "Update" verb overload; Restore submit inline green; four-hue rest-state action rows; no service container stats.
