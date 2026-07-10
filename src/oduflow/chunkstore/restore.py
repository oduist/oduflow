"""Restore a revision into a fresh directory.

Files are reconstructed in path order; consecutive small files share
chunks, so the last fetched chunk is kept as a one-slot cache. Every chunk
is integrity-verified (keyed hash) on decode. The caller performs the
atomic swap into the live location — this module never touches it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from oduflow.chunkstore.backup import list_revisions, load_revision
from oduflow.chunkstore.format import (
    StoreConfig,
    chunk_key,
    decode_chunk,
    ensure_config,
)
from oduflow.chunkstore.storage import Storage

logger = logging.getLogger("oduflow")


class _ChunkReader:
    def __init__(
        self,
        storage: Storage,
        config: StoreConfig,
        hashes: list[str],
    ) -> None:
        self.storage = storage
        self.config = config
        self.hashes = hashes
        self._cached_index: int | None = None
        self._cached_data: bytes = b""

    def get(self, index: int) -> bytes:
        if index == self._cached_index:
            return self._cached_data
        chunk_hash = self.hashes[index]
        blob = self.storage.get(chunk_key(self.config.chunk_id(chunk_hash)))
        data = decode_chunk(blob, self.config, chunk_hash)
        self._cached_index = index
        self._cached_data = data
        return data


def restore(
    storage: Storage,
    snapshot_id: str,
    revision: int | None,
    target_dir: str,
) -> dict[str, Any]:
    """Reconstruct *snapshot_id*'s *revision* (None = latest) into
    *target_dir* (created; must not already contain conflicting data)."""
    config = ensure_config(storage)
    if revision is None:
        revisions = list_revisions(storage, snapshot_id)
        if not revisions:
            raise FileNotFoundError(f"No revisions for snapshot '{snapshot_id}'")
        revision = revisions[-1]

    meta, files, hashes, _lengths = load_revision(
        storage, config, snapshot_id, revision
    )
    reader = _ChunkReader(storage, config, hashes)
    os.makedirs(target_dir, exist_ok=True)
    root = os.path.abspath(target_dir)

    restored_files = 0
    restored_bytes = 0
    deferred_modes: list[tuple[str, int]] = []

    for entry in files:
        rel = entry["path"]
        full = os.path.normpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep):
            raise ValueError(f"entry path escapes target dir: {rel!r}")
        kind = entry.get("kind")
        if kind == "dir":
            os.makedirs(full, exist_ok=True)
            deferred_modes.append((full, int(entry.get("mode", 0o755))))
            continue
        if kind == "link":
            os.makedirs(os.path.dirname(full), exist_ok=True)
            try:
                os.symlink(entry["target"], full)
            except FileExistsError:
                os.remove(full)
                os.symlink(entry["target"], full)
            continue

        os.makedirs(os.path.dirname(full), exist_ok=True)
        size = int(entry["size"])
        with open(full, "wb") as out:
            if size > 0:
                sc, so = int(entry["sc"]), int(entry["so"])
                ec, eo = int(entry["ec"]), int(entry["eo"])
                for index in range(sc, ec + 1):
                    data = reader.get(index)
                    lo = so if index == sc else 0
                    hi = eo if index == ec else len(data)
                    out.write(data[lo:hi])
        written = os.path.getsize(full)
        if written != size:
            raise ValueError(f"restored size mismatch for {rel!r}: {written} != {size}")
        os.chmod(full, int(entry.get("mode", 0o644)))
        mtime_ns = int(entry.get("mtime_ns", 0))
        if mtime_ns:
            os.utime(full, ns=(mtime_ns, mtime_ns))
        restored_files += 1
        restored_bytes += size

    # Directory modes last (children were written through them).
    for path, mode in reversed(deferred_modes):
        try:
            os.chmod(path, mode)
        except OSError:
            pass

    logger.info(
        "chunkstore restore %s/%d -> %s: %d files, %d bytes",
        snapshot_id,
        revision,
        target_dir,
        restored_files,
        restored_bytes,
    )
    return {
        "snapshot_id": snapshot_id,
        "revision": revision,
        "created_at": meta.get("created_at", ""),
        "files": restored_files,
        "bytes": restored_bytes,
    }
