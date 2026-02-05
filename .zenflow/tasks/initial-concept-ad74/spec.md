# Technical Specification - Flow

## Technical Context
- **Language**: Python 3.10+
- **Framework**: **FastMCP**
- **Dependencies**:
  - `fastmcp`: For building the MCP server.
  - `docker`: Python SDK for Docker to manage containers.
  - `python-dotenv`: For environment variable management.
- **Infrastructure**: Local Docker Desktop or Docker Engine.

## Implementation Approach
**Flow** will serve as an MCP server that exposes tools to manage ephemeral Odoo environments. Each environment will be tied to a **Zenflow** branch name.

### Key Strategies:
1. **Isolation**: Every environment will have its own Docker network and a unique name prefix based on the branch (e.g., `flow-feature-login-odoo`).
2. **Persistence**: Use Docker volumes for PostgreSQL data to persist during the life of the branch, but allow easy cleanup.
3. **Orchestration**:
   - `PostgreSQL`: Official `postgres` image.
   - `Odoo`: Official `odoo` image or custom Dockerfile if specific dependencies are needed.
4. **Tooling**: FastMCP's `@mcp.tool()` decorator will be used to expose orchestration logic to the AI Agent.

## Source Code Structure
```text
.
├── pyproject.toml             # Dependency and project management
├── .gitignore                 # Standard Python/Docker ignores
├── src/
│   └── flow/
│       ├── __init__.py
│       ├── server.py          # FastMCP server entry point
│       ├── odoo_manager.py    # Docker orchestration logic
│       └── config.py          # Default settings and constants
└── tests/
    ├── __init__.py
    ├── test_odoo_manager.py   # Unit tests for Docker logic
    └── test_server.py         # Integration tests for MCP tools
```

## Data Model / API / Interface Changes

### MCP Tools:
- **`provision_env(branch_name: str, version: str = "17.0")`**:
  - Creates a Docker network.
  - Starts a PostgreSQL container.
  - Starts an Odoo container linked to the DB.
  - Returns the local URL to access the instance.
- **`teardown_env(branch_name: str)`**:
  - Stops and removes all containers associated with the branch.
  - Deletes the network.
- **`execute_test(branch_name: str, modules: str)`**:
  - Runs `odoo-bin --test-enable --stop-after-init -i {modules}`.
  - Streams/returns logs.
- **`list_envs()`**:
  - Lists all currently running Oduist-managed environments.

## Verification Approach
1. **Linting**: Run `ruff check src/`.
2. **Type Checking**: Run `mypy src/`.
3. **Unit Testing**: 
   - Use `pytest` with `unittest.mock` to mock the Docker client.
   - Verify that container creation/deletion calls are made with correct parameters.
4. **Integration Testing**: 
   - Manually verify `fastmcp dev src/flow/server.py` to see tools in MCP Inspector.
   - (Optional) Automated test calling tools via an MCP client.
