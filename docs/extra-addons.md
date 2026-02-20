# Extra Addons Repositories

Oduflow supports mounting **extra addon repositories** (e.g. Odoo Enterprise, third-party themes) into environments. Extra repos are cloned once at the instance level and shared across environments via git worktrees.

## Architecture

```
$ODUFLOW_HOME/
  shared_repos/
    enterprise/          ← bare git clone (shared)
    custom-themes/       ← bare git clone (shared)
  workspaces/
    feature-x/
      repo/              ← main project repo (existing)
      extra/
        enterprise/      ← git worktree (branch 17.0)
        custom-themes/   ← git worktree (branch main)
```

## Setting Up Extra Repos

Clone an extra repository once (it will be available for all environments):

```bash
# Via CLI
oduflow call add_extra_repo enterprise https://github.com/odoo/enterprise.git

# Private repos — configure auth first
oduflow call setup_repo_auth https://user:PAT@github.com/odoo/enterprise.git
oduflow call add_extra_repo enterprise https://github.com/odoo/enterprise.git
```

## Using Extra Addons in Environments

When creating an environment, specify which extra repos to mount:

```bash
# Mount enterprise addons on branch 17.0
oduflow call create_environment feature-x https://github.com/company/addons.git odoo:17.0 default enterprise 17.0

# Mount multiple extra repos
oduflow call create_environment feature-x https://github.com/company/addons.git odoo:17.0 default "enterprise,custom-themes" 17.0
```

Oduflow automatically:

1. Creates a git worktree for each extra repo at the specified branch
2. Mounts the worktree **read-only** into the container as `/mnt/extra-addons-{name}`
3. Generates a merged `odoo.conf` with all extra paths added to `addons_path`

## Managing Extra Repos

```bash
# List all cloned extra repos with available branches
oduflow call list_extra_repos

# Delete an extra repo (fails if any environment references it)
oduflow call delete_extra_repo enterprise
```

Extra repos can also be managed from the **Web Dashboard** under the "Extra Addons" tab.

!!! note
    Extra addons are mounted read-only and are NOT updated by `sync_environment`. To update extra addons, delete and recreate the environment.
