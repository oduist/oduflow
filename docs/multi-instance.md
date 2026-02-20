# Multi-Instance Support

[TOC]

Oduflow supports running **multiple independent instances** on a single machine. Each instance has its own environments, templates, services, and port registry, while sharing the Docker network and PostgreSQL container.

Set `ODUFLOW_INSTANCE_ID` (1-9) and optionally `ODUFLOW_HOME` to isolate instances:

```bash
# Instance 1
export ODUFLOW_INSTANCE_ID=1
oduflow init-instance

# Instance 2 (separate terminal / process)
export ODUFLOW_INSTANCE_ID=2
export ODUFLOW_PORT=8001
oduflow init-instance
```

Databases are namespaced by instance ID (e.g. `oduflow_1_main`, `oduflow_2_main`), and containers are labeled with `oduflow.instance={ID}` for filtering.

For the full guide — lifecycle, naming conventions, shared vs. per-instance resources — see **[MULTI_INSTANCE.md](https://github.com/oduist/oduflow/blob/main/MULTI_INSTANCE.md)**.
