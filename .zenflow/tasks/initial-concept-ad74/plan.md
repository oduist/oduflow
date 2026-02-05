# Spec and build

## Configuration
- **Artifacts Path**: {@artifacts_path} → `.zenflow/tasks/{task_id}`

---

## Agent Instructions

Ask the user questions when anything is unclear or needs their input. This includes:
- Ambiguous or incomplete requirements
- Technical decisions that affect architecture or user experience
- Trade-offs that require business context

Do not make assumptions on important decisions — get clarification first.

---

## Workflow Steps

### [x] Step: Technical Specification

Assess the task's difficulty, as underestimating it leads to poor outcomes.
- easy: Straightforward implementation, trivial bug fix or feature
- medium: Moderate complexity, some edge cases or caveats to consider
- hard: Complex logic, many caveats, architectural considerations, or high-risk changes

Create a technical specification for the task that is appropriate for the complexity level:
- Review the existing codebase architecture and identify reusable components.
- Define the implementation approach based on established patterns in the project.
- Identify all source code files that will be created or modified.
- Define any necessary data model, API, or interface changes.
- Describe verification steps using the project's test and lint commands.

Save the output to `{@artifacts_path}/spec.md` with:
- Technical context (language, dependencies)
- Implementation approach
- Source code structure changes
- Data model / API / interface changes
- Verification approach

### [x] Step: Initialize Project
<!-- chat-id: b745020a-d3df-47a7-8365-5429bbd2ecfc -->
- Create `.gitignore` with Python and Docker defaults.
- Create `pyproject.toml` with `fastmcp` and `docker` dependencies.
- Setup directory structure (`src/flow`, `tests`).

### [x] Step: Implement Docker Orchestration
<!-- chat-id: e08559ba-f3cb-44f7-ab43-c9d85ba8e6ee -->
- Implement `odoo_manager.py` with functions to provision and teardown Odoo/Postgres environments.
- Use Docker labels to track branch-specific containers.
- Add unit tests for Docker logic.

### [x] Step: Implement MCP Server
<!-- chat-id: 33ee74fb-6599-4155-9f03-79fd41a7048e -->
- Create `server.py` using `FastMCP`.
- Register tools: `provision_env`, `teardown_env`, `execute_test`, `list_envs`.
- Add integration tests for MCP tools.

### [x] Step: Final Verification and Reporting
- Run `ruff` and `mypy` for code quality.
- Run all tests with `pytest`.
- Write the final report to `{@artifacts_path}/report.md`.

### [x] Step: MCP transport options
<!-- chat-id: 0ea4d212-1bd5-4b5c-8e60-ff43d04665f2 -->

Now: Starting MCP server 'Flow' with transport 'stdio'
It must be started so that I can connect remotely also.

### [x] Step: Implement Dynamic Port Allocation and Reporting
- Add `PORT_RANGE` and `EXTERNAL_HOST` to `config.py`.
- Implement dynamic port allocation in `odoo_manager.py`.
- Map host port to Odoo's 8072 port.
- Include URL in `provision_env` and `list_envs` responses.
- Verify with tests and linting.
