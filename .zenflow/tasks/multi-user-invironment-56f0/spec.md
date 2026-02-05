# Technical Specification - Multi-user Environment Support

## Technical Context
- **Language**: Python 3.10+
- **Framework**: FastMCP (MCP Server)
- **Infrastructure**: Docker
- **Dependencies**: `docker`, `fastmcp`, `hashlib` (standard library)

## Implementation Approach

### 1. User Identification
We will use an `auth_token` passed as a parameter to each tool call. To avoid exposing the token in Docker labels or resource names, we will generate a `user_id` by hashing the token:
```python
import hashlib
def get_user_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]
```

### 2. Configuration Changes (`src/flow/config.py`)
- Add `USER_LABEL = "flow.user"` to identify resources belonging to a specific user.

### 3. Manager Changes (`src/flow/odoo_manager.py`)
- Update all functions to accept `user_id`.
- `_get_resource_name`: Include `user_id` in the name: `f"{PREFIX}{user_id}-{branch_name}-{resource_type}"`.
- `_get_workspace_path`: Include `user_id` in the path: `os.path.join(WORKSPACES_DIR, user_id, branch_name)`.
- `provision_env`: 
    - Add `USER_LABEL: user_id` to labels.
- `list_envs`:
    - Add `user_id` parameter.
    - Filter containers by `USER_LABEL == user_id`.
- `teardown_env`:
    - Add `user_id` parameter.
    - Ensure it only finds resources with `USER_LABEL == user_id`.
- `execute_test`:
    - Add `user_id` parameter.

### 4. Server Changes (`src/flow/server.py`)
- Add `auth_token: str` to all tool functions.
- Calculate `user_id = get_user_id(auth_token)` at the beginning of each tool.
- Pass `user_id` to `odoo_manager` functions.

## Delivery Phases

### Phase 1: Core Logic Update
- Update `config.py` and `odoo_manager.py` to support `user_id`.
- Refactor internal helper functions to include `user_id`.

### Phase 2: Server Update
- Update `server.py` tools to require `auth_token`.
- Integrate hashing logic.

### Phase 3: Verification
- Run existing tests (they will need updates to pass the new parameters).
- Add new tests for multi-user isolation.

## Verification Approach
- **Linting**: Run `ruff check src` and `mypy src`.
- **Testing**: Run `pytest` with updated test cases.
- **Manual Verification**: Test with different tokens and ensure environments don't conflict.
