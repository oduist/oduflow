# Troubleshooting

Recovery playbooks for the operational issues most likely to hit a self-hosted
Oduflow deployment, organized by symptom. Commands assume the default data
directory `/srv/oduflow` and team `1`; adjust the paths for your setup.

Oduflow runs as **root** (it needs the Docker socket, `iptables`, host-side
`chown` and XFS project quotas). All commands below are run on the host.

---

## The server won't start / keeps restarting

`systemctl status oduflow` shows the service failing and restarting in a loop,
and `journalctl -u oduflow` ends in a traceback.

The most common cause is the **shared PostgreSQL container not being ready** when
Oduflow initializes. On startup Oduflow waits for `oduflow-db` with `pg_isready`;
if that container is not running — still starting, or crash-looping — the wait
now retries and, on timeout, fails with a clear message pointing at its logs
(older versions crashed with a raw `docker.errors.APIError: 409`).

```bash
# Is the DB container actually up?
docker inspect oduflow-db --format '{{.State.Status}} restarting={{.State.Restarting}} restarts={{.RestartCount}} exit={{.State.ExitCode}}'

# Why is PostgreSQL dying? (the decisive check)
docker logs --tail 200 oduflow-db

# Very common underlying cause — the disk is full:
df -h /srv/oduflow
```

The Oduflow version is irrelevant here — the blocker is Docker/container state,
so upgrading or downgrading Oduflow will not help until `oduflow-db` stays `Up`.
See [Disk full](#disk-full) below. Once the DB container is healthy:

```bash
systemctl restart oduflow
journalctl -u oduflow -f
```

---

## Disk full

A full disk cascades: PostgreSQL cannot write and crash-loops, new
environments fail to provision, and Odoo reports "did not become ready".

```bash
df -h /srv/oduflow          # bytes
df -i /srv/oduflow          # inodes — can be exhausted even when bytes are free
```

Find what is using space. Note that **each environment always copies its
database** (a PostgreSQL `CREATE DATABASE ... TEMPLATE` is a full copy — a few GB
per env is normal and unavoidable), while the much larger **filestore is shared
via an overlay** and should cost only a small delta per env (see
[Overlay filestore](#overlay-filestore)):

```bash
# Per-environment on-disk cost (upper layer + repo + sessions; the shared
# template filestore is NOT counted here):
du -sh /srv/oduflow/team_1/workspaces/*/filestore_upper
du -sh /srv/oduflow/team_1/workspaces/*/repo

# Templates (the shared lower layers + dumps):
du -sh /srv/oduflow/team_1/templates/*
```

To reclaim space, delete unused environments or templates through Oduflow
(`delete_environment` / `delete_template`, the dashboard, or `oduflow call`) so
databases, overlays and workspaces are torn down cleanly. **Do not** `rm -rf` a
template directory by hand while environments still use it — see
[Deleting a template fails](#deleting-a-template-fails).

---

## An environment won't start / Odoo "did not become ready"

Work down this checklist:

```bash
# 1. Is the shared DB up and accepting connections?
docker exec oduflow-db pg_isready -U odoo

# 2. Is the Odoo container running, and what does it say?
docker ps -a --filter name=<env-slug>
docker logs --tail 200 oduflow-1-<env-slug>-odoo

# 3. Is the filestore mounted and readable inside the container?
docker exec oduflow-1-<env-slug>-odoo \
  sh -c 'ls /var/lib/odoo/.local/share/Odoo/filestore/*/ 2>&1 | head'
```

If step 3 reports `Transport endpoint is not connected` or an empty filestore,
the overlay mount is broken — see [Overlay filestore](#overlay-filestore).
Otherwise the failure is usually inside Odoo (a module install/upgrade error);
read the container logs.

---

## Overlay filestore

Large template filestores are shared with `fuse-overlayfs` instead of copied.
Per environment, under `/srv/oduflow/team_1/workspaces/<env-slug>/`:

| Path | Role |
|------|------|
| `filestore` | the **merged** mountpoint (bind-mounted into the container) |
| `filestore_upper` | this env's **own** writes (the only real disk it adds) |
| `filestore_work` | fuse-overlayfs work dir (kept tiny) |

The **lower** (read-only base) layer is the template's filestore at
`/srv/oduflow/team_1/templates/<template>/filestore`, shared by every
environment created from that template.

Inspect the mounts and sizes:

```bash
# Active overlay mounts and their lower layers:
grep fuse-overlayfs /proc/mounts

# The upper layer should be small; the merged view shows the full tree
# (lower + upper) and will look ~template-sized — that is expected:
du -sh /srv/oduflow/team_1/workspaces/<env-slug>/filestore_upper   # small = healthy
du -sh /srv/oduflow/team_1/workspaces/<env-slug>/filestore         # ~= template size
```

### A broken mount (`Transport endpoint is not connected`)

The `fuse-overlayfs` process for a mount died (e.g. it was killed when the disk
filled). Detach the stale mount, then bring the environment back up through
Oduflow (which remounts it):

```bash
umount /srv/oduflow/team_1/workspaces/<env-slug>/filestore \
  || umount -l /srv/oduflow/team_1/workspaces/<env-slug>/filestore
```

### fuse-overlayfs prerequisites

```bash
which fuse-overlayfs          # install: sudo apt install fuse-overlayfs
ls -l /dev/fuse               # must exist (present by default on Ubuntu)
```

`fuse-overlayfs` is mounted with `allow_other` so the Odoo container's (non-root)
user can read it. Running Oduflow **as root** (the supported setup) needs no
further configuration. Only when running Oduflow as a **non-root user** must you
uncomment `user_allow_other` in `/etc/fuse.conf`.

!!! note "AppArmor `fusermount3` on Ubuntu 24.04+ (historical)"
    Older Oduflow unmounted overlays via the setuid `fusermount` helper, which
    the `fusermount3` AppArmor profile on Ubuntu 24.04+ **denies**, forcing a
    lazy fallback. Oduflow now unmounts with a direct root `umount` (not mediated
    by that profile), so this no longer affects root deployments. If you run a
    non-root/rootless setup and hit `apparmor="DENIED" ... fusermount3`, allow it
    with a local override — add `umount /srv/oduflow/**,` to
    `/etc/apparmor.d/local/fusermount3` and run `apparmor_parser -r
    /etc/apparmor.d/fusermount3`.

---

## Deleting a template fails

```
Cannot delete template 'X': used by environments: a, b. Delete those environments first.
```

This is intentional. A template's filestore is the overlay **lower layer** for
every environment built from it; deleting the template would pull the base out
from under those live overlays and break them. Delete the listed environments
first (or keep the template). The same guard applies to renaming a template.
