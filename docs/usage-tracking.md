# LLM Usage Tracking

Oduflow can record how much LLM work each environment costs — token usage,
wall-clock time, and which models were used — and show it on the dashboard.
Because environments are created one per branch and worked on by an AI agent,
usage is attributed per environment.

!!! note "What is (and isn't) tracked"
    Only **tokens** (input / output / cache), **time**, and **model names** are
    recorded. No monetary cost is computed or stored.

## How it works

An AI model cannot reliably measure its own token consumption mid-conversation
— only the harness (for Claude Code: the session transcript, `/cost`) knows the
real numbers. So usage is fed by a **Claude Code hook**, not self-reported by
the agent:

```
Claude Code Stop hook
  ├─ reads the session transcript → tokens per model + wall-clock time
  ├─ maps the current git branch → a per-environment capability token (UID)
  └─ POST {dashboard}/api/llm-usage   (header: X-Oduflow-Env-Uid: <uid>)
        └─ Oduflow stores it per environment → shown on the dashboard
```

The UID is an opaque per-environment capability token generated when the
environment is created. It both authenticates the request and identifies the
environment, so the hook needs no dashboard password.

!!! info "Requires the dashboard (HTTP mode)"
    The hook POSTs over HTTP, so the Oduflow dashboard must be running
    (`oduflow --transport http`) and reachable from the machine running the
    agent.

## Setup (recommended: `get_claude_hooks`)

Ask the agent to call the MCP tool **`get_claude_hooks`** for the environment:

```
get_claude_hooks(env_name="feature/login")
```

It returns ready-to-use, copy-pasteable instructions: the hook script, the
token-map entry (with the environment's UID already filled in), and the
`.claude/settings.json` snippet. The agent then creates these files itself:

1. **Hook script** → `.claude/hooks/oduflow_usage_hook.py`
2. **Token map** → `.oduflow/usage-tokens.json` at the repo root:
   ```json
   {
     "dashboard_url": "https://oduflow.example.com",
     "tokens": { "feature/login": "<usage-uid>" }
   }
   ```
   Keyed by **git branch**. The file holds a secret token — add `.oduflow/` to
   `.gitignore`.
3. **Enable the Stop hook** in `.claude/settings.json`:
   ```json
   {
     "hooks": {
       "Stop": [
         { "hooks": [ { "type": "command", "command": "python3 .claude/hooks/oduflow_usage_hook.py" } ] }
       ]
     }
   }
   ```

One hook serves the whole repository: it picks the right environment by the
current git branch. When you start work on another branch, run
`get_claude_hooks` again and add that branch → UID entry to the same map file.

### Hook configuration overrides

The bundled hook reads its config from the token map but also honours
environment variables, useful for non-standard layouts:

| Variable | Purpose |
| --- | --- |
| `ODUFLOW_USAGE_MAP` | Path to the token map (default `<repo>/.oduflow/usage-tokens.json`) |
| `ODUFLOW_DASHBOARD_URL` | Dashboard base URL (overrides `dashboard_url` in the map) |
| `ODUFLOW_ENV` | Branch key to use instead of the detected git branch |

## Viewing usage

On the dashboard, each environment card shows a compact line (total tokens,
time, models). Open **More → Usage stats** (or click the line) for the
per-model breakdown of input / output / cache tokens. The agent can read the
same numbers via `get_environment_info`.

## Idempotency, multiple sessions, and model switches

- Reporting is **idempotent per session**: the Stop hook fires on every turn
  and re-sends the session's cumulative totals, so the record is *updated*, not
  double-counted.
- **Multiple sessions** on the same environment accumulate — each session is
  tracked separately and summed.
- A model change **within** a session is handled: tokens are tracked per model.

## Persistence and lifecycle

While an environment is live, usage is stored in `.usage.json` inside its
workspace. When the environment is deleted — manually or by the auto-delete
reaper — its totals are folded into a per-team archive
(`usage.json` in the team data directory) so accounting survives the ephemeral
environment.

## REST endpoint

The hook targets a single endpoint:

| Method | Path | Auth | Body |
| --- | --- | --- | --- |
| `POST` | `/api/llm-usage` | `X-Oduflow-Env-Uid` header | `{ "session_id", "duration_seconds", "models": { "<model>": { "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens" } } } ` |

The endpoint is reachable without a dashboard login; the per-environment UID is
the credential. An invalid or missing UID returns `401`.
