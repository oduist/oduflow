# Use Cases & Workflows

## 🚀 Feature Branch Development

The most common workflow — test your changes against real production data:

```bash
# Create an environment for your feature branch
oduflow call create_environment feature-login "" default https://github.com/company/odoo-addons.git odoo:19.0

# Make changes, push to remote, then pull into the environment
oduflow call pull_and_apply feature-login
# Oduflow automatically installs/upgrades/restarts as needed

# When done, tear it down
oduflow call delete_environment feature-login
```

## 🐛 Bug Reproduction

Reproduce a production bug with real data:

```bash
# Spin up an environment with production data
oduflow call create_environment bug-12345 "" default https://github.com/company/odoo-addons.git odoo:19.0

# Debug inside the container
oduflow call run_odoo_command bug-12345 "python3 -c 'import odoo; ...'"

# Check the database directly
oduflow call run_odoo_command bug-12345 "psql -h oduflow-db -U odoo -d oduflow_bug-12345 -c 'SELECT * FROM sale_order WHERE id=42;'"
```

## 🧪 Module Testing

Run Odoo tests in an isolated environment:

```bash
oduflow call create_environment test-suite "" default https://github.com/company/odoo-addons.git odoo:19.0
oduflow call run_odoo_tests test-suite sale_custom,invoice_custom
oduflow call delete_environment test-suite
```

## 🌱 Greenfield Project (No Production Database)

Start a new Odoo project from scratch:

```bash
# Generate a clean template with common modules
oduflow init-template --odoo-image odoo:19.0 --template-name default --modules base,web,contacts,sale,purchase,stock

# Now create environments that start with your customized setup
oduflow call create_environment dev "" default https://github.com/company/new-project.git odoo:19.0
```

## 🔄 Multiple Odoo Versions

Manage environments across different Odoo versions using named templates:

```bash
# Set up templates for different versions
oduflow init-template --odoo-image odoo:15.0 --template-name v15
oduflow init-template --odoo-image odoo:19.0 --template-name v19

# Create environments targeting specific versions
oduflow call create_environment legacy-fix "" v15 https://github.com/company/v15-addons.git odoo:15.0
oduflow call create_environment new-feature "" v19 https://github.com/company/v19-addons.git odoo:19.0
```

## 🤖 AI-Assisted Development

Let your AI coding agent manage Odoo environments. Configure your MCP client (Cursor, Cline, Amp) to connect to `http://<host>:8000/mcp`, then:

> *"Create an Odoo 19 environment for the `feature-payment-gateway` branch from our repo. Install the `sale` and `payment` modules, then run the tests."*

The agent will call the appropriate MCP tools in sequence:

1. `create_environment` → provision the environment
2. `install_odoo_modules` → install the requested modules
3. `run_odoo_tests` → run the test suite
4. Report results back

### Connecting Your Agent to Oduflow MCP

Add the Oduflow MCP server to your agent's configuration. The exact format depends on the client:

```json
{
  "mcpServers": {
    "oduflow": {
      "type": "http",
      "url": "https://<your-oduflow-host>/mcp",
      "headers": {
        "Authorization": "Bearer test"
      }
    }
  }
}
```

Replace `<your-oduflow-host>` with your Oduflow server address (e.g. `localhost:8000` or `oduflow.example.com`). The Bearer token must match the `auth_token` configured for your team in `oduflow.toml`.

### Recommended Agent Rule (Cursor / Windsurf / Amp)

You can add the following rule to your AI coding agent to automate environment lifecycle management:

```
---
description: "Manage Odoo dev environments via the Oduflow MCP server"
alwaysApply: true
---
```

**Initialization**

1. **Check**: Call `list_environments`. If an environment matching the current branch already exists, use it.
2. **Create**: If not, use `create_environment`:
   - `branch`: `<current branch>`
   - `repo_url`: `<repository URL>` (HTTPS)
   - `odoo_image`: `odoo19_prod` (IMPORTANT: always use this image)
3. **Auth**: On a 401/403 error, suggest `setup_repo_auth`.
4. When creating or finding an existing environment, add the environment URL to `{@artifacts_path}/report.md`.

**Sync & Work Cycle**

1. **Push**: Run `git push` when the task is complete.
2. **Pull**: After every `push` (yours or user-requested), ALWAYS call `pull_and_apply`.
3. **Automation**: The Flow server decides whether a restart or module upgrade is needed. You do NOT need to call `restart_environment` or `upgrade_odoo_modules`.

**Teardown**

- Only delete the environment via `delete_environment` if the task status is **Done** or **Canceled**.
- Do not recreate the environment to fix errors without the user's consent.

**Important**

- One task = one branch = one environment.
- Always display the environment URL to the user when creating an environment.

## 📊 Environment with Auxiliary Services

Set up a full-stack development environment:

```bash
# Create the Odoo environment
oduflow call create_environment dev "" default https://github.com/company/odoo-addons.git odoo:19.0

# Add Redis for caching
oduflow call create_service redis redis:7 6379

# Add Meilisearch for full-text search
oduflow call create_service meilisearch getmeili/meilisearch:v1.6 7700 "" "MEILI_MASTER_KEY=devkey123"
```

All services share the `oduflow-net` Docker network and can communicate using container names as hostnames (e.g. `oduflow-svc-redis:6379`).

## 🔧 CI/CD Pipeline Integration

Use `oduflow call` in your CI pipeline:

```yaml
# .github/workflows/test.yml
steps:
  - name: Create test environment
    run: oduflow call create_environment ci-${{ github.sha }} "" default ${{ github.repository }} odoo:19.0

  - name: Install and test
    run: |
      oduflow call install_odoo_modules ci-${{ github.sha }} my_module
      oduflow call run_odoo_tests ci-${{ github.sha }} my_module

  - name: Cleanup
    if: always()
    run: oduflow call delete_environment ci-${{ github.sha }}
```

## 📦 Importing a Template from Odoo or Another Workspace

You can create a template from a running Odoo instance, from a manual database backup, or by copying a template directory from another Oduflow instance.

**Directly from a running Odoo instance (recommended):**

The easiest way — Oduflow downloads the backup, extracts it, auto-detects the Odoo version, and loads the template in one command:

```bash
oduflow import-template https://my-odoo.example.com master_password --template-name default
```

Options:

- `--db-name <db>` — specify the database name (auto-detected if only one DB exists)
- `--template-name <name>` — template profile name (default: `default`)
- `--without-filestore` — request a database-only ZIP backup without filestore files

This is also available as an MCP tool (`import_template_from_odoo`) for AI agents; pass `without_filestore=true` for a database-only import.

**From Odoo Database Manager (manual):**

1. Go to `/web/database/manager` in your Odoo instance
2. Download a backup — **make sure to include the filestore** (the checkbox must be enabled, otherwise the template will be missing all attachments, images, and assets)
3. Extract the archive — it contains a `dump.sql` file and a `filestore/` directory
4. Place them into the template directory:

```bash
mkdir -p {data_dir}/team_{ID}/templates/myproject
# Copy or move the extracted files
cp dump.sql {data_dir}/team_{ID}/templates/myproject/
cp -r filestore {data_dir}/team_{ID}/templates/myproject/
```

5. Load the template into PostgreSQL:

```bash
oduflow reload-template myproject
```

**From another Oduflow workspace:**

Simply copy the entire template directory and reload:

```bash
cp -r /other/oduflow/templates/myproject {data_dir}/team_{ID}/templates/myproject
oduflow reload-template myproject
```

!!! warning
    The SQL dump is loaded into the shared PostgreSQL instance by `reload-template`. Without this step, the template will appear in the list but show **DB NOT LOADED** and cannot be used to create environments.

## 🏗️ Template Evolution

Evolve your template as the project grows:

```bash
# 1. Create an environment for template changes
oduflow call create_environment template-update "" default https://github.com/company/odoo-addons.git odoo:19.0

# 2. Install new modules
oduflow call install_odoo_modules template-update accounting,hr,project

# 3. Verify everything works
oduflow call run_odoo_tests template-update accounting,hr,project

# 4. Save as the new template
oduflow call save_as_template template-update default

# 5. All future environments will include these modules pre-installed
```
