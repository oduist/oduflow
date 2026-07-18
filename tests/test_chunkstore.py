"""Chunkstore tests — all against LocalStorage, no S3/Docker required."""

import datetime
import os
import random

import pytest

from oduflow import chunkstore
from oduflow.chunkstore.backup import list_revisions
from oduflow.chunkstore.chunker import Chunker, derive_table
from oduflow.chunkstore.format import (
    ChunkCorruptedError,
    decode_chunk,
    encode_chunk,
    ensure_config,
)
from oduflow.chunkstore.prune import parse_keep, select_revisions_to_keep
from oduflow.chunkstore.storage import CountingStorage, LocalStorage

# Small chunk parameters so tests work on kilobytes, not megabytes.
SMALL = dict(min_size=1024, avg_size=4096, max_size=16384)


def _chunk_all(chunker: Chunker, data: bytes, block: int = 8192) -> list[bytes]:
    chunks = []
    for i in range(0, len(data), block):
        chunks.extend(chunker.update(data[i : i + block]))
    chunks.extend(chunker.flush())
    return chunks


class TestChunker:
    def test_deterministic_for_same_seed(self):
        data = random.Random(1).randbytes(200_000)
        a = _chunk_all(Chunker(b"seed-a", **SMALL), data)
        b = _chunk_all(Chunker(b"seed-a", **SMALL), data, block=333)
        assert a == b

    def test_different_seed_different_boundaries(self):
        data = random.Random(2).randbytes(200_000)
        a = _chunk_all(Chunker(b"seed-a", **SMALL), data)
        b = _chunk_all(Chunker(b"seed-b", **SMALL), data)
        assert [len(c) for c in a] != [len(c) for c in b]

    def test_size_bounds(self):
        data = random.Random(3).randbytes(500_000)
        chunks = _chunk_all(Chunker(b"s", **SMALL), data)
        assert b"".join(chunks) == data
        for chunk in chunks[:-1]:
            assert SMALL["min_size"] <= len(chunk) <= SMALL["max_size"]
        assert len(chunks[-1]) <= SMALL["max_size"]

    def test_shift_resistance(self):
        # Inserting bytes at the start must not re-chunk the whole stream:
        # most chunks reappear identically (content-defined boundaries).
        data = random.Random(4).randbytes(300_000)
        original = set(_chunk_all(Chunker(b"s", **SMALL), data))
        shifted = set(_chunk_all(Chunker(b"s", **SMALL), b"XY" + data))
        common = original & shifted
        assert len(common) >= len(original) * 0.6

    def test_table_derivation_stable(self):
        assert derive_table(b"x") == derive_table(b"x")
        assert derive_table(b"x") != derive_table(b"y")
        assert len(derive_table(b"x")) == 256

    def test_avg_must_be_power_of_two(self):
        with pytest.raises(ValueError, match="power of two"):
            Chunker(b"s", min_size=100, avg_size=3000, max_size=9000)


class TestChunkFormat:
    def test_roundtrip_compressible(self, tmp_path):
        storage = LocalStorage(str(tmp_path))
        config = ensure_config(storage)
        payload = b"hello world " * 1000
        blob = encode_chunk(payload)
        assert len(blob) < len(payload)  # zstd kicked in
        assert decode_chunk(blob, config, config.chunk_hash(payload)) == payload

    def test_roundtrip_incompressible(self, tmp_path):
        storage = LocalStorage(str(tmp_path))
        config = ensure_config(storage)
        payload = random.Random(5).randbytes(10_000)
        blob = encode_chunk(payload)
        assert decode_chunk(blob, config, config.chunk_hash(payload)) == payload

    def test_corruption_detected(self, tmp_path):
        storage = LocalStorage(str(tmp_path))
        config = ensure_config(storage)
        payload = b"data" * 100
        blob = bytearray(encode_chunk(payload))
        blob[-1] ^= 0xFF
        with pytest.raises(ChunkCorruptedError):
            decode_chunk(bytes(blob), config, config.chunk_hash(payload))

    def test_config_created_once(self, tmp_path):
        storage = LocalStorage(str(tmp_path))
        a = ensure_config(storage)
        b = ensure_config(storage)
        assert a == b
        assert len(a.seed) == 32


def _make_tree(root, spec: dict[str, bytes]) -> None:
    for rel, content in spec.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(content)


def _read_tree(root) -> dict[str, bytes]:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as f:
                out[rel] = f.read()
    return out


@pytest.fixture
def small_config_storage(tmp_path, monkeypatch):
    """LocalStorage whose auto-created config uses tiny chunk sizes."""
    from oduflow.chunkstore import format as fmt

    monkeypatch.setattr(fmt, "META_MIN_SIZE", 256)
    monkeypatch.setattr(fmt, "META_AVG_SIZE", 1024)
    monkeypatch.setattr(fmt, "META_MAX_SIZE", 4096)
    # backup.py imported the names at module load; patch there too. (The
    # package __init__ re-exports the backup *function* under the same
    # name, so resolve the module via importlib.)
    import importlib

    backup_mod = importlib.import_module("oduflow.chunkstore.backup")

    monkeypatch.setattr(backup_mod, "META_MIN_SIZE", 256)
    monkeypatch.setattr(backup_mod, "META_AVG_SIZE", 1024)
    monkeypatch.setattr(backup_mod, "META_MAX_SIZE", 4096)

    storage = LocalStorage(str(tmp_path / "store"))
    ensure_config(storage)
    # Rewrite config with small data-chunk parameters.
    import json

    raw = json.loads(storage.get("config").decode())
    raw.update({"min_size": 1024, "avg_size": 4096, "max_size": 16384})
    storage.put("config", json.dumps(raw).encode())
    return storage


class TestBackupRestore:
    def test_roundtrip_mixed_tree(self, small_config_storage, tmp_path):
        rng = random.Random(7)
        spec = {
            "empty.txt": b"",
            "small/a.bin": rng.randbytes(100),
            "small/b.bin": rng.randbytes(300),
            "big/blob.bin": rng.randbytes(60_000),
            "nested/deep/dir/file.txt": b"hello\n" * 50,
            "unicode-файл.dat": rng.randbytes(2000),
        }
        src = tmp_path / "src"
        _make_tree(str(src), spec)
        os.makedirs(src / "empty-dir")
        os.symlink("small/a.bin", src / "link.bin")
        os.chmod(src / "small" / "a.bin", 0o600)

        result = chunkstore.backup(str(src), small_config_storage, "erp")
        assert result.revision == 1
        assert result.files == len(spec)

        dst = tmp_path / "dst"
        info = chunkstore.restore(small_config_storage, "erp", None, str(dst))
        assert info["files"] == len(spec)
        assert _read_tree(str(src)) == _read_tree(str(dst))
        assert os.path.isdir(dst / "empty-dir")
        assert os.readlink(dst / "link.bin") == "small/a.bin"
        assert os.stat(dst / "small" / "a.bin").st_mode & 0o777 == 0o600
        # mtimes preserved
        assert (
            os.stat(dst / "big" / "blob.bin").st_mtime_ns
            == os.stat(src / "big" / "blob.bin").st_mtime_ns
        )

    def test_incremental_skips_unchanged(self, small_config_storage, tmp_path):
        rng = random.Random(8)
        spec = {f"f{i}.bin": rng.randbytes(5000) for i in range(10)}
        src = tmp_path / "src"
        _make_tree(str(src), spec)

        chunkstore.backup(str(src), small_config_storage, "erp")

        counting = CountingStorage(small_config_storage)
        # Change ONE file.
        with open(src / "f3.bin", "wb") as f:
            f.write(rng.randbytes(5000))
        result = chunkstore.backup(str(src), counting, "erp")
        assert result.revision == 2
        assert result.unchanged_files == 9
        # Only the changed file's chunks + metadata chunks are uploaded.
        assert result.new_chunks <= 8
        assert counting.counts["put"] <= 10  # chunks + revision file

        # Both revisions restore correctly.
        dst = tmp_path / "dst"
        chunkstore.restore(small_config_storage, "erp", 2, str(dst))
        assert _read_tree(str(src)) == _read_tree(str(dst))

    def test_cross_snapshot_dedup(self, small_config_storage, tmp_path):
        # Two productions with identical filestores share chunks.
        rng = random.Random(9)
        spec = {f"blob{i}": rng.randbytes(20_000) for i in range(5)}
        src_a = tmp_path / "a"
        src_b = tmp_path / "b"
        _make_tree(str(src_a), spec)
        _make_tree(str(src_b), spec)

        first = chunkstore.backup(str(src_a), small_config_storage, "prod-a")
        counting = CountingStorage(small_config_storage)
        second = chunkstore.backup(str(src_b), counting, "prod-b")
        # prod-b re-reads the files (different mtimes) but uploads almost
        # nothing: all data chunks already exist.
        assert second.new_chunks < first.new_chunks / 2
        assert second.uploaded_bytes < first.uploaded_bytes / 2

    def test_deleted_files_disappear(self, small_config_storage, tmp_path):
        src = tmp_path / "src"
        _make_tree(str(src), {"keep.txt": b"keep", "drop.txt": b"drop"})
        chunkstore.backup(str(src), small_config_storage, "erp")
        os.remove(src / "drop.txt")
        chunkstore.backup(str(src), small_config_storage, "erp")

        dst = tmp_path / "dst"
        chunkstore.restore(small_config_storage, "erp", 2, str(dst))
        assert _read_tree(str(dst)) == {"keep.txt": b"keep"}

    def test_property_random_trees(self, small_config_storage, tmp_path):
        rng = random.Random(11)
        src = tmp_path / "src"
        spec = {}
        for i in range(40):
            depth = rng.randint(0, 3)
            parts = [f"d{rng.randint(0, 3)}" for _ in range(depth)]
            name = "/".join(parts + [f"file{i}.bin"])
            spec[name] = rng.randbytes(rng.choice([0, 1, 100, 5000, 30_000]))
        _make_tree(str(src), spec)
        chunkstore.backup(str(src), small_config_storage, "erp")

        # Mutate: delete some, change some, add some.
        for i, name in enumerate(sorted(spec)):
            if i % 7 == 0:
                os.remove(os.path.join(str(src), name))
            elif i % 5 == 0:
                with open(os.path.join(str(src), name), "wb") as f:
                    f.write(rng.randbytes(rng.randint(1, 10_000)))
        _make_tree(str(src), {"new/extra.bin": rng.randbytes(12_345)})
        chunkstore.backup(str(src), small_config_storage, "erp")

        dst = tmp_path / "dst"
        chunkstore.restore(small_config_storage, "erp", 2, str(dst))
        assert _read_tree(str(src)) == _read_tree(str(dst))


class TestRetention:
    def _revisions(self, days_ago: list[int], now):
        return [
            (i + 1, now - datetime.timedelta(days=d)) for i, d in enumerate(days_ago)
        ]

    def test_keep_all_recent(self):
        now = datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc)
        revs = self._revisions([0, 1, 2, 3], now)
        kept = select_revisions_to_keep(revs, parse_keep(["1:7"]), now)
        assert kept == {1, 2, 3, 4}

    def test_daily_thinning(self):
        now = datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc)
        # Two revisions on the same old day: only one survives the 1:7 rule.
        revs = [
            (1, now - datetime.timedelta(days=10, hours=2)),
            (2, now - datetime.timedelta(days=10, hours=1)),
            (3, now - datetime.timedelta(days=1)),
        ]
        kept = select_revisions_to_keep(revs, parse_keep(["1:7"]), now)
        assert 3 in kept
        assert len(kept & {1, 2}) == 1

    def test_zero_interval_drops(self):
        now = datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc)
        revs = self._revisions([400, 300, 1], now)
        kept = select_revisions_to_keep(revs, parse_keep(["0:365", "1:7"]), now)
        assert 1 not in kept  # older than 365d, dropped
        assert 3 in kept

    def test_newest_always_kept(self):
        now = datetime.datetime(2026, 7, 10, tzinfo=datetime.timezone.utc)
        revs = self._revisions([500], now)
        kept = select_revisions_to_keep(revs, parse_keep(["0:365"]), now)
        assert kept == {1}


class TestPrune:
    def _backup_days(self, storage, src, snapshot_id, monkeypatch_days):
        """Create a revision whose created_at is `monkeypatch_days` ago."""
        import json

        result = chunkstore.backup(str(src), storage, snapshot_id)
        key = f"snapshots/{snapshot_id}/{result.revision}"
        meta = json.loads(storage.get(key).decode())
        created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=monkeypatch_days
        )
        meta["created_at"] = created.isoformat()
        storage.put(key, json.dumps(meta).encode())
        return result

    def test_two_step_prune_and_restore_safety(self, small_config_storage, tmp_path):
        rng = random.Random(12)
        storage = small_config_storage
        src = tmp_path / "src"
        _make_tree(str(src), {"a.bin": rng.randbytes(30_000)})
        self._backup_days(storage, src, "erp", 30)

        # Rev 2: different content, recent.
        with open(src / "a.bin", "wb") as f:
            f.write(rng.randbytes(30_000))
        self._backup_days(storage, src, "erp", 0)

        keep = parse_keep(["0:7"])  # drop everything older than 7 days
        result1 = chunkstore.prune(storage, keep=keep)
        assert "erp/1" in result1.deleted_revisions
        assert result1.fossilized_chunks > 0
        assert result1.deleted_chunks == 0  # step 2 hasn't run for these

        # Fossils exist but latest revision still restores fine.
        dst = tmp_path / "dst"
        chunkstore.restore(storage, "erp", None, str(dst))
        assert _read_tree(str(dst)) == _read_tree(str(src))

        # A new backup happens (moves latest revision forward)...
        with open(src / "a.bin", "ab") as f:
            f.write(b"more")
        self._backup_days(storage, src, "erp", 0)

        # ...and a prune two hours "later" permanently deletes the fossils.
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=2
        )
        result2 = chunkstore.prune(storage, keep=keep, now=later)
        assert result2.deleted_chunks > 0
        assert not [k for k in storage.list("chunks/") if k.endswith(".fsl")]

        # Everything still restores.
        dst2 = tmp_path / "dst2"
        chunkstore.restore(storage, "erp", None, str(dst2))
        assert _read_tree(str(dst2)) == _read_tree(str(src))

    def test_fossil_resurrection(self, small_config_storage, tmp_path):
        """A chunk fossilized in step 1 that a NEW revision references again
        is resurrected in step 2, not deleted."""
        rng = random.Random(13)
        storage = small_config_storage
        src = tmp_path / "src"
        original = rng.randbytes(30_000)
        _make_tree(str(src), {"a.bin": original})
        self._backup_days(storage, src, "erp", 30)

        with open(src / "a.bin", "wb") as f:
            f.write(rng.randbytes(30_000))
        self._backup_days(storage, src, "erp", 0)

        # Step 1 fossilizes rev-1-only chunks.
        chunkstore.prune(storage, keep=parse_keep(["0:7"]))
        fossils = [k for k in storage.list("chunks/") if k.endswith(".fsl")]
        assert fossils

        # The original content comes BACK (new revision references the same
        # chunks — but they're fossilized, so backup re-uploads them).
        with open(src / "a.bin", "wb") as f:
            f.write(original)
        self._backup_days(storage, src, "erp", 0)

        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=2
        )
        chunkstore.prune(storage, keep=parse_keep(["0:7"]), now=later)
        # No fossils left; latest revision restores the original content.
        assert not [k for k in storage.list("chunks/") if k.endswith(".fsl")]
        dst = tmp_path / "dst"
        chunkstore.restore(storage, "erp", None, str(dst))
        assert _read_tree(str(dst)) == {"a.bin": original}


class TestLocalStorage:
    def test_key_escape_rejected(self, tmp_path):
        storage = LocalStorage(str(tmp_path))
        with pytest.raises(ValueError, match="escapes"):
            storage.get("../outside")

    def test_list_revisions(self, tmp_path):
        storage = LocalStorage(str(tmp_path))
        storage.put("snapshots/erp/1", b"{}")
        storage.put("snapshots/erp/3", b"{}")
        storage.put("snapshots/erp/2", b"{}")
        assert list_revisions(storage, "erp") == [1, 2, 3]


class TestPruneKeepRevisions:
    def test_explicit_keep_revisions(self, small_config_storage, tmp_path):
        """Caller-driven retention keeps exactly the named revisions (plus
        the newest) — the lockstep mode used by backup_ops."""
        rng = random.Random(21)
        storage = small_config_storage
        src = tmp_path / "src"
        _make_tree(str(src), {"a.bin": rng.randbytes(20_000)})
        for _ in range(3):
            with open(src / "a.bin", "wb") as f:
                f.write(rng.randbytes(20_000))
            chunkstore.backup(str(src), storage, "erp")
        assert list_revisions(storage, "erp") == [1, 2, 3]

        chunkstore.prune(storage, keep_revisions={"erp": {2}})
        assert list_revisions(storage, "erp") == [2, 3]

        # Both survivors restore.
        for rev in (2, 3):
            dst = tmp_path / f"dst{rev}"
            chunkstore.restore(storage, "erp", rev, str(dst))

    def test_prune_requires_a_policy(self, small_config_storage):
        with pytest.raises(ValueError, match="keep"):
            chunkstore.prune(small_config_storage)
