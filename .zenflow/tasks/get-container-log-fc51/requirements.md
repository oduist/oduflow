# Requirements: get_env_odoo_log

## Overview
The goal is to implement a new MCP tool `get_env_odoo_log` that allows users to retrieve the last N lines of logs from an Odoo container associated with a specific branch/environment.

## User Stories
- As a developer, I want to see the recent logs of my Odoo environment to debug issues.
- As a developer, I want to specify how many lines of logs I want to retrieve.

## Requirements

### MCP Tool: `get_env_odoo_log`
- **Function Name**: `get_env_odoo_log`
- **Arguments**:
  - `branch_name` (string, required): The name of the branch/environment.
  - `n_lines` (integer, optional, default=100): The number of recent log lines to retrieve.
- **Return Value**: A string containing the log lines or an error message.

### Functional Requirements
1.  **Authentication**: The tool must use the existing `_get_user_id()` mechanism to identify the user and ensure they only access their own environments.
2.  **Container Identification**: The tool must correctly identify the Odoo container associated with the given `user_id` and `branch_name`.
3.  **Log Retrieval**:
    - Use the Docker API to fetch logs from the Odoo container.
    - Support fetching only the last `n_lines`.
    - Handle cases where the container might not be running or doesn't exist.
4.  **Formatting**: The output should be clearly formatted as a string.

### Error Handling
- If the branch/environment does not exist, return a descriptive error message.
- If the Odoo container cannot be found, return a descriptive error message.
- Handle any Docker API errors gracefully.

## Technical Constraints
- Must be implemented within the existing `src/flow/server.py` and `src/flow/odoo_manager.py` structure.
- Must use the `docker` Python library for log retrieval.
- Must follow the existing coding style and conventions (e.g., type hints, error handling patterns).
