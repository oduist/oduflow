# Full SDD workflow

## Configuration
- **Artifacts Path**: {@artifacts_path} → `.zenflow/tasks/{task_id}`

---

## Workflow Steps

### [x] Step: Requirements
<!-- chat-id: 3629236e-954a-4048-ae2a-86e1975fe229 -->

Create a Product Requirements Document (PRD) based on the feature description.

1. Review existing codebase to understand current architecture and patterns
2. Analyze the feature definition and identify unclear aspects
3. Ask the user for clarifications on aspects that significantly impact scope or user experience
4. Make reasonable decisions for minor details based on context and conventions
5. If user can't clarify, make a decision, state the assumption, and continue

Save the PRD to `{@artifacts_path}/requirements.md`.

### [x] Step: Implementation

1. [x] Implement `get_env_odoo_log` in `src/flow/odoo_manager.py`
2. [x] Register the tool in `src/flow/server.py`
3. [x] Add a test case in `tests/test_odoo_manager.py`
4. [x] Verify with tests and linting
