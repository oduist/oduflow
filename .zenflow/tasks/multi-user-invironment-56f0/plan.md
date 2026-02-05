# Implementation Plan - Multi-user Environment Support (Header-based)

### [x] Step: Requirements Analysis
- [x] Identified `fastmcp.server.dependencies.get_http_headers` for header access.
- [x] Updated requirements to reflect `API_KEY` header usage.

### [x] Step: Implement Phase 1: Server Refactoring
- [x] Remove `auth_token` from tool parameters in `src/flow/server.py`.
- [x] Implement `_get_user_id()` to extract `API_KEY` from headers.
- [x] Default transport to `http` in `main()`.

### [x] Step: Implement Phase 2: Manager Verification
- [x] Verified `src/flow/odoo_manager.py` uses `user_id` correctly for isolation.
- [x] Confirmed no hashing is needed as `API_KEY` is the `user_id`.

### [x] Step: Implement Phase 3: Verification
- [x] Updated `tests/test_server.py` to mock headers.
- [x] Ran `pytest` and verified all tests pass.
- [x] Verified tool signatures in server code.

### [x] Step: Phase 4: Environment-based API Key Verification
- [x] Define `FLOW_API_KEYS` in environment (comma-separated list).
- [x] Update `_get_user_id()` in `src/flow/server.py` to verify the header against the allowed list.
- [x] Update tests to handle the new verification logic.
