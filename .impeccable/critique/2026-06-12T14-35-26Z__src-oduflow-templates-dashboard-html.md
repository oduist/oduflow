---
target: src/oduflow/templates/dashboard.html
total_score: 25
p0_count: 0
p1_count: 3
timestamp: 2026-06-12T14-35-26Z
slug: src-oduflow-templates-dashboard-html
---
# Design Critique: Oduflow Dashboard (src/oduflow/templates/dashboard.html)

## Design Health Score (Nielsen heuristics)

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 2 | Minutes-long ops (create/update/recreate) show a frozen "Creating..." with no stage/elapsed/output; `.spinner` CSS exists but is never wired; poll re-render can re-enable buttons mid-operation |
| 2 | Match System / Real World | 3 | "Update" means 3 different things (env / service / repo); "partial" unglossed — operator language is "1/2 containers running" |
| 3 | User Control and Freedom | 3 | Cancel/Esc/focus-return everywhere; no cancel for in-flight ops, no undo path after delete |
| 4 | Consistency and Standards | 3 | Protect = buried menu item on envs but loud amber toggle on repos; volumes reuse running/exited badges for in-use/unused (alert red on a healthy state); Restore submit inline green |
| 5 | Error Prevention | 3 | Excellent confirm copy + protected-env guards; BUT confirmDialog focuses the destructive button (Enter deletes a database); no required-field markers |
| 6 | Recognition Rather Than Recall | 3 | Restart/Sync/Update/Recreate taxonomy explained only inside confirm dialogs; template tags and PARTIAL badge have no tooltips |
| 7 | Flexibility and Efficiency | 2 | Enter never submits any modal form (no <form>); no URL routing (F5 loses tab); no bulk actions; no log follow/tail (`.logs-btn.active` is dead CSS); no global shortcuts |
| 8 | Aesthetic and Minimalist Design | 3 | Earned density mostly right; rest-state 4-hue action rows compete with status badges; "Refresh [60]s Refresh" duplication; 6-7-item meta rows |
| 9 | Error Recovery | 2 | Sync-result modal is excellent; everything else fails through a 3-second toast with raw backend strings — unreadable, uncopyable, no trace |
| 10 | Help and Documentation | 1 | No help affordance anywhere; no docs link; placeholders are the entire help system |
| **Total** | | **25/40** | **Acceptable — significant improvements needed** |

## Anti-Patterns Verdict

**LLM assessment: not AI slop.** Passes the full DON'T list; bespoke Console token system; disciplined state-driven vocabulary. Unfinished corners ≠ slop: duplicate-class rendering bug in Create modal branch inputs, orphaned "—" credential status tag, Refresh label duplication.

**Deterministic scan (CLI, 3 warnings):** overused-font (Geist Mono — brand-mandated, identity-preservation wins: false positive by intent), flat-type-hierarchy (10.5/11/11.5/12px micro-steps — legitimate signal of typographic drift), em-dash-overuse (11 in file; many are title separators "Logs — branch" in JS strings, true body-copy count lower).

**In-page overlay scan (31 findings):** 16× gpt-thin-border-wide-shadow (1px border + 48-64px shadow = the documented Raised Edge Rule on modals/menus/toasts — intentional, user-requested, false positive by design); 14× tiny-text (10.5-11.5px meta/hints — agrees with flat-type-hierarchy: the micro-scale crept down); 1× skipped-heading (h1 → h3 "Saved Presets", missing h2 — real).

**Where A and B agree:** typographic micro-scale drift (too many sizes below 12px). **Detector caught what review missed:** heading-level skip; quantified tiny-text spread. **Review caught what detector can't:** all three P1s (feedback over time), the duplicate-class bug, esc() not escaping quotes.

## Overall Impression

The visual system is genuinely Oduflow's own — dense, token-true, not slop. What separates it from a Linear-grade operator console is not aesthetics but **feedback honesty over time**: the machine is shown at rest, but goes silent exactly when it is working, failing, or being driven by agents. All three P1s are one theme.

## What's Working

1. **The sync-result modal embodies "show the machine"**: action chip, module tags, changed files, raw output, exit code — the product's emotional peak and the template the rest of the feedback system should grow from.
2. **State-driven controls with discipline**: lifecycle toggle, protected-env guards with explanations, in-use volume locks. "State is sacred" is implemented.
3. **Quietly competent plumbing**: focus trap + Esc layering with terminal exception, tablist + roving tabindex, reduced-motion, re-render that preserves menus/selection, vendored assets for air-gap.

## Priority Issues

- **[P1] Long-running operations have no progress feedback; busy state leaks on re-render.** Create/Update/Recreate freeze on a button label for minutes; `.spinner` never used; poll re-render re-enables buttons mid-op (double-fire risk). Fix: JS busyOps map consulted by renderEnvironments; in-card status chip with elapsed time; async create with staged output (sync-result style). Command: /impeccable craft (operations feedback) or /impeccable harden.
- **[P1] Partial-environment flow dead-ends.** PARTIAL badge unexplained; `[exited]` token dim and unhighlighted; Logs modal cannot target a specific container (endpoint takes none); no per-container restart. Fix: "1/2 containers running" gloss, red exited tokens, container picker in logs toolbar + ?container= param. Command: /impeccable harden.
- **[P1] Errors are transient and unreadable.** All failures route through a 3-second toast with raw backend text; no persistence/copy/history. Fix: error toasts persist until dismissed + Copy; lifecycle failures reuse sync-result modal pattern. Command: /impeccable harden.
- **[P2] Create Environment modal: flat 8-field wall + duplicate class attribute bug.** Per-repo branch inputs carry two class attributes (cr-extra-branch-input … branch-input) — second dropped, input renders full-width unstyled (confirmed in live DOM and source lines 1911/1950). Fix: merge classes; group form (Source / Template & Addons / Advanced collapsed); mark required. Command: /impeccable harden + /impeccable layout.
- **[P2] Keyboard/SR gaps at the two highest-stakes moments.** Unlicensed badge (only path to activation) is a non-focusable span with onclick; confirmDialog focuses the destructive button so Enter confirms deletion. Fix: badge → button; danger confirms focus Cancel. Command: /impeccable harden.

## Persona Red Flags

**Alex (power user):** Enter doesn't submit forms; no log tail/follow; no bulk ops (stop all / prune exited = N×confirms); no deep links (F5 → Environments); no global shortcuts despite keyboard-ready foundation.

**Sam (screen reader / keyboard):** cannot activate license (non-focusable span) — task-blocking; Enter-focus on destructive confirm; "Refresh s" accessible name on the interval spinbutton; polling status changes never announced; ✓/✗ symbols lead validation results.

**Ilya (Odoo integrator, agents do the work — from PRODUCT.md):** dashboard shows state, never history — agent-triggered operations (the majority) leave zero visible trace; locks invisible ("busy" toast with no who/why/how-long); 60s polling lags the agents; Notes is the only human/agent context channel.

## Minor Observations

- esc() does not escape double quotes → attribute-injection risk via branch names (agents create branch names).
- Token violations: ANSI map + _XTERM_THEME cursor + usageColor() HSL outside tokens; --cyan/--violet/--rose declared, never used (signal-channel system unimplemented).
- Volumes "unused" wears alert-red exited badge (red for a healthy state).
- Templates tab: no delete/save-as despite MCP tools existing; empty state doesn't teach.
- Services lack Stop; service containers show no CPU/RAM stats.
- maskValue() preserves secret length (#### count).
- Credentials show bare "—" tag before validation; reads as a glitch.
- Dead CSS: .spinner, .logs-btn.active. Header "Refresh [60]s / Refresh" duplication.
- Created timestamps: locale string with seconds; relative time fits register.
- h1 → h3 heading skip ("Saved Presets"). Em-dashes in UI copy (titles use " — ").
- Mobile solid; system bar eats ~90px of 844px; word-break splits URLs mid-word.

## Questions to Consider

1. What if the console had an **operations ledger** — a persistent drawer streaming every lifecycle op (human and MCP-agent) with status, duration, expandable sync-result-style output? One structure solves all three P1s and the agent-invisibility problem.
2. What if **PARTIAL were a diagnosis, not a label** — badge expands to "gevent exited 12m ago (code 137) — view logs · restart container"?
3. What if the four lifecycle verbs collapsed into **one "Apply" with a preview**, reusing git_analysis classification — the same mental model agents use via pull_and_apply?
