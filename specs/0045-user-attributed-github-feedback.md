# 0045 — User-attributed GitHub feedback

**Status:** Adopted
**Type:** Product capability / MCP + dashboard
**First introduced:** `litnimax/github-issue-feedback` branch (2026-08-11)
**Key code today:** `feedback.py`, `server.py` (`report_issue`), `web_ui.py`, dashboard feedback dialog, `.github/ISSUE_TEMPLATE/`

## Context

Users and coding agents need a low-friction way to report bugs, request
features, and send product feedback while they are working in Oduflow. Filing
issues automatically would require Oduflow to hold GitHub credentials, decide
whose identity owns the report, and publish user-entered content without a
final human review. A private relay would solve attribution differently but
would add another operated service and obscure where the report goes.

Diagnostic context is useful, but Oduflow installations contain identifying
and sensitive values such as hostnames, team names, repository URLs, branches,
and database names. A feedback convenience must not silently disclose them.

## Decision

Oduflow builds a prefilled GitHub Issue Form URL but never submits the issue.
The user opens the form, reviews or edits every field, and creates the issue
from their own GitHub account. The dashboard and the `report_issue` MCP tool
share one URL builder and diagnostics policy.

Only non-identifying runtime facts are added automatically: Oduflow and Python
versions, platform family and architecture, transport, routing mode, and
whether production support is enabled. Deployment identifiers and customer
data are excluded. Free-form title and details are bounded so the encoded URL
remains usable, and issue-form YAML owns labels because ordinary GitHub users
cannot reliably set labels through the new-issue URL.

## How it works (macro)

- Separate issue forms define bug, feature, and general-feedback fields and
  labels. Their stable field IDs are the prefill contract.
- `feedback.py` selects the form, collects safe diagnostics, percent-encodes
  the fields, and truncates free-form content to a conservative URL budget.
- The authenticated dashboard endpoint validates the request and returns the
  URL. The browser opens it in a new tab so the user remains in control of
  publication.
- The MCP `report_issue` tool returns the same URL and makes explicit that
  nothing is sent until the user presses GitHub's create button.

## Consequences

- Reports are attributable without storing a GitHub token in Oduflow, and the
  reporter gets a final privacy and accuracy review before publication.
- The feature has no automatic delivery, retry, or telemetry semantics. It is
  distinct from [[0019-telemetry]]: this channel is deliberate, public, and
  user-attributed.
- Users can still paste secrets into free-form fields, so the UI and issue
  forms warn that reports are public.
- Long reports may be truncated in the prefilled URL; the user can paste the
  remainder directly into GitHub.
- The flow depends on GitHub's issue-form and URL-prefill behavior, while the
  dashboard exposure follows [[0005-web-dashboard-and-rest-api]] and agent
  access follows [[0009-agent-guidance-system]].

## History

- `litnimax/github-issue-feedback` / PR #175 (2026-08-11) — shared prefilled
  issue-link builder, dashboard dialog and endpoint, MCP tool, and issue forms.
