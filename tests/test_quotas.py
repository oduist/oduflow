from unittest.mock import MagicMock, call, mock_open, patch

import pytest

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

    def test_quota_of_one_gb_is_enforced(self, tmp_path, monkeypatch):
        # `disk_quota_gb > 0` selects the teams to enforce; 1 GB is the smallest
        # real quota and must not be treated as "unset".
        monkeypatch.setattr(quotas, "quota_support", lambda s: (True, "/srv"))
        with patch.object(quotas, "_xfs_quota") as xfs:
            quotas.apply_all(_settings(tmp_path, quota_gb=1))

        assert call("/srv", "limit -p bhard=1g 1001") in xfs.call_args_list

    def test_teams_without_a_quota_are_filtered_out(self, tmp_path, monkeypatch):
        quota_team = TeamSettings(
            team_id="1", data_dir=str(tmp_path / "team_1"), disk_quota_gb=5
        )
        free_team = TeamSettings(
            team_id="2",
            data_dir=str(tmp_path / "team_2"),
            disk_quota_gb=0,
            port_range_start=50100,
            port_range_end=50200,
        )
        settings = Settings(
            base_data_dir=str(tmp_path), teams={"1": quota_team, "2": free_team}
        )
        monkeypatch.setattr(quotas, "quota_support", lambda s: (True, "/srv"))
        applied = []
        monkeypatch.setattr(
            quotas,
            "apply_team_disk_quota",
            lambda s, team, mp: applied.append(team.team_id),
        )

        quotas.apply_all(settings)

        assert applied == ["1"]


class TestReadMounts:
    def test_parses_proc_mounts(self):
        content = (
            "/dev/sda1 / ext4 rw,relatime 0 0\n"
            "/dev/sdb1 /srv xfs rw,prjquota 0 0\n"
        )
        with patch("builtins.open", mock_open(read_data=content)) as opened:
            mounts = quotas._read_mounts()

        opened.assert_called_once_with("/proc/mounts")
        assert mounts == [
            ("/", "ext4", "rw,relatime"),
            ("/srv", "xfs", "rw,prjquota"),
        ]

    def test_short_lines_are_ignored(self):
        content = "garbage\n/dev/sdb1 /srv xfs rw 0 0\nalso bad\n"
        with patch("builtins.open", mock_open(read_data=content)):
            assert quotas._read_mounts() == [("/srv", "xfs", "rw")]

    def test_four_fields_are_enough(self):
        # device, mountpoint, fstype, options — the dump/pass columns at the
        # end are optional as far as this parser is concerned.
        content = "/dev/sda1 / ext4 rw\n/dev/sdb1 /srv xfs\n"
        with patch("builtins.open", mock_open(read_data=content)):
            assert quotas._read_mounts() == [("/", "ext4", "rw")]

    def test_absent_proc_mounts_yields_empty_list(self):
        # macOS and other non-Linux hosts have no /proc/mounts at all.
        with patch("builtins.open", side_effect=OSError("no /proc")):
            assert quotas._read_mounts() == []


class TestFindMount:
    def _mounts(self, monkeypatch, mounts):
        monkeypatch.setattr(quotas, "_read_mounts", lambda: mounts)

    def test_picks_the_longest_matching_mountpoint(self, monkeypatch):
        self._mounts(
            monkeypatch,
            [
                ("/", "ext4", "rw"),
                ("/srv", "ext4", "rw"),
                ("/srv/oduflow", "xfs", "rw,prjquota"),
            ],
        )

        assert quotas._find_mount("/srv/oduflow/team_1") == (
            "/srv/oduflow",
            "xfs",
            "rw,prjquota",
        )

    def test_longest_match_wins_regardless_of_order(self, monkeypatch):
        # /proc/mounts is not sorted; the deepest mountpoint must win even when
        # it is listed first.
        self._mounts(
            monkeypatch,
            [("/srv/oduflow", "xfs", "rw,prjquota"), ("/", "ext4", "rw")],
        )

        assert quotas._find_mount("/srv/oduflow/team_1")[0] == "/srv/oduflow"

    def test_exact_mountpoint_match(self, monkeypatch):
        self._mounts(monkeypatch, [("/", "ext4", "rw"), ("/srv", "xfs", "rw")])

        assert quotas._find_mount("/srv") == ("/srv", "xfs", "rw")

    def test_sibling_prefix_is_not_a_match(self, monkeypatch):
        # /srv-backup must not match the /srv mount just because the string
        # starts with it — the separator is what makes it a child path.
        self._mounts(monkeypatch, [("/", "ext4", "rw"), ("/srv", "xfs", "rw")])

        assert quotas._find_mount("/srv-backup/data") == ("/", "ext4", "rw")

    def test_root_mount_matches_everything(self, monkeypatch):
        self._mounts(monkeypatch, [("/", "ext4", "rw")])

        assert quotas._find_mount("/anywhere/at/all") == ("/", "ext4", "rw")

    def test_no_mounts_yields_none(self, monkeypatch):
        self._mounts(monkeypatch, [])

        assert quotas._find_mount("/srv") is None


class TestXfsQuotaInvocation:
    def test_builds_the_expected_command(self, monkeypatch):
        seen = {}

        def _run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return MagicMock(returncode=0, stderr="")

        monkeypatch.setattr(quotas.subprocess, "run", _run)

        quotas._xfs_quota("/srv", "limit -p bhard=10g 1001")

        assert seen["argv"] == [
            "xfs_quota",
            "-x",
            "-c",
            "limit -p bhard=10g 1001",
            "/srv",
        ]
        assert seen["kwargs"]["capture_output"] is True
        assert seen["kwargs"]["timeout"] == 60

    def test_nonzero_exit_raises_with_stderr(self, monkeypatch):
        monkeypatch.setattr(
            quotas.subprocess,
            "run",
            lambda *a, **kw: MagicMock(returncode=1, stderr="  not a mount point \n"),
        )

        with pytest.raises(RuntimeError, match="not a mount point"):
            quotas._xfs_quota("/srv", "limit -p bhard=10g 1001")

    def test_success_is_silent(self, monkeypatch):
        monkeypatch.setattr(
            quotas.subprocess,
            "run",
            lambda *a, **kw: MagicMock(returncode=0, stderr=""),
        )

        assert quotas._xfs_quota("/srv", "print") is None
