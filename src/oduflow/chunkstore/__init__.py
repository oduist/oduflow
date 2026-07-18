"""Chunkstore — a duplicacy-inspired, lock-free CDC backup engine.

Clean-room implementation written from the published algorithm description
(content-defined chunking with a rolling hash, content-addressed chunk
storage, lock-free deduplication via existence checks, two-step fossil
collection for pruning). No code was translated from duplicacy — its
license covers its code, not the algorithm.

Used to back up production Odoo filestores to S3 with cross-revision and
cross-production deduplication. The storage backend is pluggable
(:class:`LocalStorage` for tests and local targets, ``S3Storage`` in
:mod:`oduflow.s3_client` for the real thing).

Public API::

    from oduflow import chunkstore

    result = chunkstore.backup(source_dir, storage, snapshot_id="erp")
    chunkstore.restore(storage, "erp", result.revision, target_dir)
    chunkstore.prune(storage, keep=[(30, 180), (7, 30), (1, 7)])
"""

from oduflow.chunkstore.backup import BackupResult, backup, list_revisions
from oduflow.chunkstore.prune import PruneResult, prune
from oduflow.chunkstore.restore import restore
from oduflow.chunkstore.storage import LocalStorage, Storage

__all__ = [
    "BackupResult",
    "LocalStorage",
    "PruneResult",
    "Storage",
    "backup",
    "list_revisions",
    "prune",
    "restore",
]
