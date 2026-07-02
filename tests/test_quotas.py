from unittest.mock import call, patch

from oduflow import quotas
from oduflow.settings import Settings, TeamSettings


def _team(tmp_path, quota_gb=10) -> TeamSettings:
    return TeamSettings(
        team_id="1", data_dir=str(tmp_path / "team_1"), disk_quota_gb=quota_gb
    )


def _settings(tmp_path, quota_gb=10) -> Settings:
    return Settings(base_data_dir=str(tmp_path), teams={"1": _team(tmp_path, quota_gb)})


class TestQuotaSupport:
    def test_non_linux(self, monkeypatch):
        monkeypatch.setattr(quotas.sys, "platform", "darwin")
        ok, reason = quotas.quota_support(Settings(base_data_dir="/srv"))
        assert not ok
        assert "not linux" in reason

    def test_missing_xfs_quota_binary(self, monkeypatch):
        monkeypatch.setattr(quotas.sys, "platform", "linux")
        monkeypatch.setattr(quotas.shutil, "which", lambda name: None)
        ok, reason = quotas.quota_support(Settings(base_data_dir="/srv"))
        assert not ok
        assert "xfsprogs" in reason

    def _support(self, monkeypatch, mounts, path="/srv/oduflow"):
        monkeypatch.setattr(quotas.sys, "platform", "linux")
        monkeypatch.setattr(quotas.shutil, "which", lambda name: "/usr/sbin/xfs_quota")
        monkeypatch.setattr(quotas, "_read_mounts", lambda: mounts)
        return quotas.quota_support(Settings(base_data_dir=path))

    def test_not_xfs(self, monkeypatch):
        ok, reason = self._support(
            monkeypatch, [("/", "ext4", "rw,relatime"), ("/srv", "ext4", "rw")]
        )
        assert not ok
        assert "not xfs" in reason

    def test_xfs_without_prjquota(self, monkeypatch):
        ok, reason = self._support(monkeypatch, [("/srv", "xfs", "rw,relatime")])
        assert not ok
        assert "prjquota" in reason

    def test_supported_picks_longest_mount(self, monkeypatch):
        ok, mountpoint = self._support(
            monkeypatch,
            [("/", "ext4", "rw"), ("/srv", "xfs", "rw,prjquota")],
        )
        assert ok
        assert mountpoint == "/srv"


class TestProjectIds:
    def test_sequential_and_stable(self, tmp_path):
        settings = _settings(tmp_path)
        first = quotas._project_id_for(settings, "1")
        second = quotas._project_id_for(settings, "acme")
        assert (first, second) == (1001, 1002)
        # Stable across calls (persisted).
        assert quotas._project_id_for(settings, "1") == 1001


class TestApplyAll:
    def test_disabled_when_no_quota_set(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(quotas, "quota_support", lambda s: called.append(1))
        quotas.apply_all(_settings(tmp_path, quota_gb=0))
        assert called == []  # support not even probed

    def test_unsupported_logs_and_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(quotas, "quota_support", lambda s: (False, "no xfs"))
        with patch.object(quotas, "apply_team_disk_quota") as apply_one:
            quotas.apply_all(_settings(tmp_path))
        apply_one.assert_not_called()

    def test_applies_project_and_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(quotas, "quota_support", lambda s: (True, "/srv"))
        with patch.object(quotas, "_xfs_quota") as xfs:
            quotas.apply_all(_settings(tmp_path, quota_gb=25))

        team_dir = str(tmp_path / "team_1")
        pg_dir = str(tmp_path / "pg_tablespaces" / "team_1")
        assert xfs.call_args_list == [
            call("/srv", f"project -s -p {team_dir} 1001"),
            call("/srv", f"project -s -p {pg_dir} 1001"),
            call("/srv", "limit -p bhard=25g 1001"),
        ]
        # Both trees exist (created if missing so the project can be set up).
        assert (tmp_path / "pg_tablespaces" / "team_1").is_dir()

    def test_one_team_failure_does_not_block_others(self, tmp_path, monkeypatch):
        t1 = TeamSettings(
            team_id="1", data_dir=str(tmp_path / "team_1"), disk_quota_gb=5
        )
        t2 = TeamSettings(
            team_id="2",
            data_dir=str(tmp_path / "team_2"),
            disk_quota_gb=5,
            port_range_start=50100,
            port_range_end=50200,
        )
        settings = Settings(base_data_dir=str(tmp_path), teams={"1": t1, "2": t2})
        monkeypatch.setattr(quotas, "quota_support", lambda s: (True, "/srv"))
        applied = []

        def apply_one(settings_, team, mountpoint):
            if team.team_id == "1":
                raise RuntimeError("boom")
            applied.append(team.team_id)

        monkeypatch.setattr(quotas, "apply_team_disk_quota", apply_one)
        quotas.apply_all(settings)
        assert applied == ["2"]
