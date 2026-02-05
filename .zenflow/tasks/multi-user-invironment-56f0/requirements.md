# Requirements: Multi-User Environment for Flow MCP

## Overview
The goal is to support multiple users sharing the same Flow MCP server instance by isolating their Odoo environments based on an `API_KEY` provided in HTTP headers.

## Functional Requirements
1.  **Authentication via Header**: The MCP server must extract the `API_KEY` from HTTP request headers.
2.  **User ID**: The `API_KEY` provided in the header is used directly as the `user_id`.
3.  **Transport Type**: The server operates using SSE (Server-Sent Events) transport, typically served over HTTPS.
4.  **Environment Isolation**:
    *   All Docker resources (containers, networks) created by a user must be tagged with their `user_id`.
    *   Workspaces on the host filesystem must be isolated by the `user_id`.
5.  **Scoped Operations**:
    *   `list_envs` must only return environments belonging to the current `user_id`.
    *   `teardown_env` must only allow deleting environments belonging to the current `user_id`.
    *   `execute_test` must only allow executing tests in environments belonging to the current `user_id`.
    *   `provision_env` must associate new resources with the current `user_id`.
6.  **Tool Cleanliness**: MCP tools should NOT include `user_id` or `auth_token` as parameters in their signature.

## Technical Assumptions
*   Clients will specify the `API_KEY` in the headers (e.g., `"API_KEY": "YOUR_API_KEY"`).
*   The server will use `fastmcp.server.dependencies.get_http_headers()` to retrieve headers.

## Success Criteria
*   User A cannot see or manage User B's environments.
*   User A can create an environment with the same branch name as User B without conflict.
*   Tool signatures are clean and do not expose authentication details as parameters.
