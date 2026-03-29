import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from backup_projects.cli import (
    BackupError,
    _local_backup_sync_roots,
    _sync_local_destination_paths,
    configure_logging,
    default_mode_from_config,
    force_sync_enabled,
    load_config,
    max_workers_from_config,
    merge_keep_different_only,
    merge_tgz_rotate,
    normalize_sources,
    parse_rsync_extra,
    prune_tgz_archives,
    prune_tgz_duplicate_hashes,
    resolve_log_path,
    run_from_config,
    tgz_datetime_suffix_enabled,
)

GT = "/default/backup"
CFG0: dict = {}


def test_normalize_sources_strings():
    raw = ["/a/b/foo", "/c/d"]
    assert normalize_sources(raw, "update", GT, CFG0) == [
        ("/a/b/foo", "foo", [(GT, "update", False, None, False)]),
        ("/c/d", "d", [(GT, "update", False, None, False)]),
    ]


def test_normalize_sources_objects():
    raw = [{"path": "/x/y", "name": "custom"}, {"path": "/z"}]
    assert normalize_sources(raw, "copy", GT, CFG0) == [
        ("/x/y", "custom", [(GT, "copy", False, None, False)]),
        ("/z", "z", [(GT, "copy", False, None, False)]),
    ]


def test_normalize_sources_per_source_mode():
    raw = [
        {"path": "/a", "mode": "tgz"},
        "/b",
        {"path": "/c", "name": "see", "mode": "copy"},
    ]
    assert normalize_sources(raw, "update", GT, CFG0) == [
        ("/a", "a", [(GT, "tgz", False, None, False)]),
        ("/b", "b", [(GT, "update", False, None, False)]),
        ("/c", "see", [(GT, "copy", False, None, False)]),
    ]


def test_normalize_sources_per_source_target():
    raw = [{"path": "/a", "target": "/mnt/usb"}]
    assert normalize_sources(raw, "update", GT, CFG0) == [
        ("/a", "a", [("/mnt/usb", "update", False, None, False)]),
    ]


def test_normalize_sources_targets_list():
    raw = [{"path": "/a", "targets": ["/t1", "/t2"]}]
    assert normalize_sources(raw, "update", GT, CFG0) == [
        ("/a", "a", [("/t1", "update", False, None, False), ("/t2", "update", False, None, False)]),
    ]


def test_normalize_sources_targets_mixed_modes():
    raw = [
        {
            "path": "/a",
            "mode": "update",
            "targets": [
                "/inc",
                {"target": "/snap", "mode": "copy"},
                {"target": "/arc"},
            ],
        }
    ]
    assert normalize_sources(raw, "tgz", GT, CFG0) == [
        (
            "/a",
            "a",
            [
                ("/inc", "update", False, None, False),
                ("/snap", "copy", False, None, False),
                ("/arc", "update", False, None, False),
            ],
        ),
    ]


def test_normalize_sources_targets_object_needs_target_key():
    with pytest.raises(BackupError, match="non-empty 'target'"):
        normalize_sources(
            [{"path": "/a", "targets": [{"mode": "copy"}]}],
            "update",
            GT,
            CFG0,
        )


def test_normalize_sources_targets_entry_mode_invalid():
    with pytest.raises(BackupError, match="targets\\[\\]\\.mode"):
        normalize_sources(
            [{"path": "/a", "targets": [{"target": "/x", "mode": "bad"}]}],
            "update",
            GT,
            CFG0,
        )


def test_normalize_sources_targets_precedence_over_target():
    raw = [{"path": "/a", "target": "/alone", "targets": ["/x", "/y"]}]
    assert normalize_sources(raw, "update", GT, CFG0) == [
        ("/a", "a", [("/x", "update", False, None, False), ("/y", "update", False, None, False)]),
    ]


def test_normalize_sources_empty_targets_rejected():
    with pytest.raises(BackupError, match="targets must be a non-empty"):
        normalize_sources([{"path": "/a", "targets": []}], "update", GT, CFG0)


def test_normalize_sources_invalid_global_target():
    with pytest.raises(BackupError, match="global target"):
        normalize_sources(["/a"], "update", "", CFG0)


def test_normalize_sources_invalid_default_mode():
    with pytest.raises(BackupError, match="default_mode"):
        normalize_sources(["/a"], "nope", GT, CFG0)


def test_normalize_sources_invalid_source_mode():
    with pytest.raises(BackupError, match="source mode"):
        normalize_sources([{"path": "/a", "mode": "bad"}], "update", GT, CFG0)


def test_normalize_sources_invalid_type():
    with pytest.raises(BackupError, match="sources must be a list"):
        normalize_sources("not-a-list", "update", GT, CFG0)


def test_normalize_sources_bad_entry():
    with pytest.raises(BackupError, match="sources entries"):
        normalize_sources([123], "update", GT, CFG0)


def test_normalize_sources_enable_false_skips():
    raw = [
        {"path": "/a", "enable": False},
        "/b",
        {"path": "/c", "enable": True},
    ]
    assert normalize_sources(raw, "update", GT, CFG0) == [
        ("/b", "b", [(GT, "update", False, None, False)]),
        ("/c", "c", [(GT, "update", False, None, False)]),
    ]


def test_normalize_sources_enable_invalid_type():
    with pytest.raises(BackupError, match="source enable must be a boolean"):
        normalize_sources([{"path": "/a", "enable": "no"}], "update", GT, CFG0)


def test_normalize_sources_all_disabled_empty_list():
    assert normalize_sources([{"path": "/a", "enable": False}], "update", GT, CFG0) == []


def test_run_from_config_all_sources_disabled_raises(tmp_path: Path):
    with pytest.raises(BackupError, match="sources must be a non-empty list"):
        run_from_config(
            {
                "target": str(tmp_path),
                "sources": [{"path": str(tmp_path), "enable": False}],
            }
        )


def test_normalize_sources_targets_non_string_non_dict():
    with pytest.raises(BackupError, match="targets entries must be"):
        normalize_sources([{"path": "/a", "targets": [1]}], "update", GT, CFG0)


def test_max_workers_from_config_defaults():
    assert max_workers_from_config({}, 0) == 1
    assert max_workers_from_config({}, 3) == 3
    assert max_workers_from_config({}, 20) == 8


def test_max_workers_from_config_explicit():
    assert max_workers_from_config({"max_workers": 3}, 10) == 3
    assert max_workers_from_config({"max_workers": 100}, 4) == 4


def test_max_workers_from_config_invalid():
    with pytest.raises(BackupError, match="max_workers"):
        max_workers_from_config({"max_workers": 0}, 5)
    with pytest.raises(BackupError, match="max_workers"):
        max_workers_from_config({"max_workers": True}, 5)


def test_merge_tgz_rotate_global():
    cfg = {"rotate": True, "max_count": 3}
    assert merge_tgz_rotate(cfg, None, None) == (True, 3)


def test_merge_tgz_rotate_source_overrides():
    cfg = {"rotate": True, "max_count": 3}
    src = {"rotate": False}
    assert merge_tgz_rotate(cfg, src, None) == (False, None)


def test_merge_tgz_rotate_target_overrides():
    cfg = {}
    src = {"rotate": True, "max_count": 5}
    tgt = {"max_count": 2}
    assert merge_tgz_rotate(cfg, src, tgt) == (True, 2)


def test_merge_tgz_rotate_requires_max_count():
    with pytest.raises(BackupError, match="max_count"):
        merge_tgz_rotate({"rotate": True}, None, None)


def test_merge_tgz_rotate_invalid_bool():
    with pytest.raises(BackupError, match="rotate must be a boolean"):
        merge_tgz_rotate({"rotate": "yes", "max_count": 2}, None, None)


def test_merge_keep_different_only_defaults_and_overrides():
    assert merge_keep_different_only({}, None, None) is False
    assert merge_keep_different_only({"keep_different_only": True}, None, None) is True
    src = {"keep_different_only": False}
    assert merge_keep_different_only({"keep_different_only": True}, src, None) is False
    assert merge_keep_different_only({}, {"keep_different_only": True}, None) is True
    tgt = {"keep_different_only": True}
    assert merge_keep_different_only({}, {"keep_different_only": False}, tgt) is True


def test_merge_keep_different_only_invalid():
    with pytest.raises(BackupError, match="keep_different_only"):
        merge_keep_different_only({"keep_different_only": "yes"}, None, None)


def test_normalize_sources_keep_different_only():
    raw = [{"path": "/a", "mode": "tgz", "keep_different_only": True}]
    assert normalize_sources(raw, "update", GT, CFG0) == [
        ("/a", "a", [(GT, "tgz", False, None, True)]),
    ]
    cfg_g = {"keep_different_only": True}
    assert normalize_sources(["/x"], "update", GT, cfg_g) == [
        ("/x", "x", [(GT, "update", False, None, True)]),
    ]


def test_normalize_sources_rotate_on_source():
    raw = [{"path": "/a", "mode": "tgz", "rotate": True, "max_count": 4}]
    assert normalize_sources(raw, "update", GT, CFG0) == [
        ("/a", "a", [(GT, "tgz", True, 4, False)]),
    ]


def test_normalize_sources_rotate_on_target_entry():
    raw = [
        {
            "path": "/a",
            "mode": "tgz",
            "targets": [
                {"target": "/t1", "rotate": True, "max_count": 2},
            ],
        }
    ]
    assert normalize_sources(raw, "update", GT, CFG0) == [
        ("/a", "a", [("/t1", "tgz", True, 2, False)]),
    ]


def test_prune_tgz_duplicate_hashes_removes_same_bytes(tmp_path: Path):
    root = tmp_path
    data = b"same-archive-bytes"
    older = root / "app-20260101_120000.tgz"
    newer = root / "app-20260102_130000.tgz"
    older.write_bytes(data)
    newer.write_bytes(data)
    prune_tgz_duplicate_hashes(root, "app", newer)
    assert not older.exists()
    assert newer.is_file()


def test_prune_tgz_duplicate_hashes_keeps_when_hash_differs(tmp_path: Path):
    root = tmp_path
    a = root / "app-20260101_120000.tgz"
    b = root / "app-20260102_130000.tgz"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    prune_tgz_duplicate_hashes(root, "app", b)
    assert a.is_file() and b.is_file()


def test_prune_tgz_archives_keeps_newest(tmp_path: Path):
    root = tmp_path
    names = [
        "arch-20260101_100000.tgz",
        "arch-20260102_100000.tgz",
        "arch-20260103_100000.tgz",
        "arch-20260104_100000.tgz",
    ]
    for i, nm in enumerate(names):
        p = root / nm
        p.write_bytes(b"x")
        ts = 1000 + i * 100
        os.utime(p, (ts, ts))
    prune_tgz_archives(root, "arch", 2)
    left = {p.name for p in root.glob("*.tgz")}
    assert left == {"arch-20260103_100000.tgz", "arch-20260104_100000.tgz"}


def test_default_mode_from_config_prefers_default_mode():
    assert default_mode_from_config({"default_mode": "tgz"}) == "tgz"


def test_default_mode_from_config_legacy_mode_key():
    assert default_mode_from_config({"mode": "copy"}) == "copy"


def test_default_mode_from_config_default_mode_over_mode():
    assert default_mode_from_config({"default_mode": "tgz", "mode": "copy"}) == "tgz"


def test_parse_rsync_extra():
    assert parse_rsync_extra({}) == []
    assert parse_rsync_extra({"rsync_extra": "--exclude .git"}) == ["--exclude", ".git"]
    assert parse_rsync_extra({"rsync_extra": ["--exclude", ".cache"]}) == ["--exclude", ".cache"]


def test_parse_rsync_extra_shlex_preserves_spaces_in_quotes():
    assert parse_rsync_extra({"rsync_extra": '--exclude "/tmp/foo bar/x"'}) == [
        "--exclude",
        "/tmp/foo bar/x",
    ]


def test_parse_rsync_extra_invalid():
    with pytest.raises(BackupError, match="rsync_extra"):
        parse_rsync_extra({"rsync_extra": 1})


def test_tgz_datetime_suffix_enabled():
    assert tgz_datetime_suffix_enabled({}) is False
    assert tgz_datetime_suffix_enabled({"tgz_datetime_suffix": True}) is True


def test_tgz_datetime_suffix_invalid_type():
    with pytest.raises(BackupError, match="tgz_datetime_suffix"):
        tgz_datetime_suffix_enabled({"tgz_datetime_suffix": "yes"})


def test_force_sync_enabled():
    assert force_sync_enabled({}) is False
    assert force_sync_enabled({"force_sync": False}) is False
    assert force_sync_enabled({"force_sync": True}) is True


def test_force_sync_enabled_invalid_type():
    with pytest.raises(BackupError, match="force_sync"):
        force_sync_enabled({"force_sync": "yes"})


def test_local_backup_sync_roots_local_tasks_and_flag_base():
    tasks = [
        ("/src", "n", "/mnt/usb/backup", "update", False, None, False),
        ("/src2", "n2", "/other", "copy", False, None, False),
    ]
    roots = _local_backup_sync_roots(tasks, "/var/flag-base")
    assert roots == sorted(
        [
            Path("/mnt/usb/backup").resolve(),
            Path("/other").resolve(),
            Path("/var/flag-base").resolve(),
        ],
        key=lambda p: str(p),
    )


def test_local_backup_sync_roots_skips_remote():
    tasks = [("/a", "b", "user@host:/path", "update", False, None, False)]
    assert _local_backup_sync_roots(tasks, "user@host:/root") == []


def test_sync_local_destination_paths_noop_empty():
    _sync_local_destination_paths([])


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_run_from_config_force_sync_passes_local_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    recorded: list[list[Path]] = []

    def capture(roots: list) -> None:
        recorded.append(list(roots))

    monkeypatch.setattr("backup_projects.cli._sync_local_destination_paths", capture)
    src = tmp_path / "src" / "proj"
    src.mkdir(parents=True)
    (src / "f.txt").write_text("x", encoding="utf-8")
    dst = tmp_path / "usb" / "dst"
    flag_base = tmp_path / "state"
    flag_base.mkdir(parents=True)
    cfg = {
        "target": str(flag_base),
        "default_mode": "update",
        "sources": [{"path": str(src), "name": "proj", "target": str(dst)}],
        "success_flag": ".ok",
        "log_file": False,
        "force_sync": True,
    }
    configure_logging(None, verbose=False)
    run_from_config(cfg)
    assert len(recorded) == 1
    got = {p.resolve() for p in recorded[0]}
    assert got == {dst.resolve(), flag_base.resolve()}


def test_load_config_roundtrip(tmp_path: Path):
    p = tmp_path / "c.yaml"
    data = {
        "target": "/tmp",
        "default_mode": "update",
        "sources": ["/a"],
        "log_file": False,
        "log_filename": "app.log",
    }
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_config(p) == data


def test_load_config_missing(tmp_path: Path):
    with pytest.raises(BackupError, match="config not found"):
        load_config(tmp_path / "nope.yaml")


def test_resolve_log_path_cli_overrides():
    cfg = {"log_file": False, "log_filename": "x.log"}
    assert resolve_log_path(cfg, "/tmp/x.log") == Path("/tmp/x.log")


def test_resolve_log_path_false():
    cfg = {"log_file": False}
    assert resolve_log_path(cfg, None) is None


def test_resolve_log_path_default_when_key_missing():
    cfg = {"target": "/x"}
    p = resolve_log_path(cfg, None)
    assert p is not None
    assert p.name == "backup.log"
    assert p.parent.name == "backup-projects"


def test_resolve_log_path_log_filename():
    cfg = {"log_filename": "my-projects.log"}
    p = resolve_log_path(cfg, None)
    assert p.name == "my-projects.log"


def test_resolve_log_path_full_path_ignores_log_filename():
    cfg = {"log_filename": "ignored.log", "log_file": "/var/log/other.log"}
    p = resolve_log_path(cfg, None)
    assert p == Path("/var/log/other.log")


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_run_from_config_skips_disabled_source(tmp_path: Path):
    on = tmp_path / "on" / "d"
    on.mkdir(parents=True)
    (on / "x").write_text("1", encoding="utf-8")
    off = tmp_path / "off" / "d"
    off.mkdir(parents=True)
    (off / "y").write_text("2", encoding="utf-8")
    dst = tmp_path / "dst"
    log_f = tmp_path / "en.log"
    cfg = {
        "target": str(dst),
        "default_mode": "update",
        "sources": [
            {"path": str(off), "name": "off", "enable": False},
            {"path": str(on), "name": "on"},
        ],
        "success_flag": ".ok",
        "log_file": str(log_f),
    }
    configure_logging(log_f, verbose=False)
    run_from_config(cfg)
    assert (dst / "on" / "x").read_text() == "1"
    assert not (dst / "off").exists()


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_run_from_config_paths_with_spaces(tmp_path: Path):
    src = tmp_path / "source dir" / "my project"
    src.mkdir(parents=True)
    (src / "f.txt").write_text("ok", encoding="utf-8")
    dst = tmp_path / "backup root"
    dst.mkdir(parents=True)
    log_f = tmp_path / "sp.log"
    flag = tmp_path / "flagbase"
    flag.mkdir(parents=True)
    cfg = {
        "target": str(flag),
        "default_mode": "update",
        "sources": [{"path": str(src), "name": "my project", "target": str(dst)}],
        "success_flag": ".ok",
        "log_file": str(log_f),
    }
    configure_logging(log_f, verbose=False)
    run_from_config(cfg)
    assert (dst / "my project" / "f.txt").read_text() == "ok"


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_run_from_config_update_local(tmp_path: Path):
    src = tmp_path / "src" / "proj"
    src.mkdir(parents=True)
    (src / "f.txt").write_text("data", encoding="utf-8")
    dst = tmp_path / "dst"
    log_f = tmp_path / "run.log"
    cfg = {
        "target": str(dst),
        "default_mode": "update",
        "sources": [{"path": str(src), "name": "proj"}],
        "success_flag": ".ok",
        "log_file": str(log_f),
    }
    configure_logging(log_f, verbose=False)
    run_from_config(cfg)
    assert (dst / "proj" / "f.txt").read_text() == "data"
    assert (dst / ".ok").is_file()
    assert log_f.is_file()
    assert "starting backup" in log_f.read_text(encoding="utf-8")


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_run_from_config_mixed_modes(tmp_path: Path):
    u = tmp_path / "u" / "p"
    u.mkdir(parents=True)
    (u / "a").write_text("1", encoding="utf-8")
    t = tmp_path / "t" / "q"
    t.mkdir(parents=True)
    (t / "b").write_text("2", encoding="utf-8")
    dst = tmp_path / "out"
    log_f = tmp_path / "mixed.log"
    cfg = {
        "target": str(dst),
        "default_mode": "update",
        "sources": [
            {"path": str(u), "name": "inc"},
            {"path": str(t), "name": "arch", "mode": "tgz"},
        ],
        "success_flag": ".done",
        "log_file": str(log_f),
    }
    configure_logging(log_f, verbose=False)
    run_from_config(cfg)
    assert (dst / "inc" / "a").read_text() == "1"
    tgzs = list((dst).glob("arch-*.tgz"))
    assert len(tgzs) == 1
    assert re.fullmatch(r"arch-\d{8}_\d{6}\.tgz", tgzs[0].name)
    assert (dst / ".done").is_file()


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_run_from_config_tgz_datetime_suffix(tmp_path: Path):
    t = tmp_path / "t" / "q"
    t.mkdir(parents=True)
    (t / "b").write_text("2", encoding="utf-8")
    dst = tmp_path / "out"
    log_f = tmp_path / "tgzfmt.log"
    cfg = {
        "target": str(dst),
        "default_mode": "tgz",
        "tgz_datetime_suffix": True,
        "sources": [{"path": str(t), "name": "arch"}],
        "success_flag": ".done",
        "log_file": str(log_f),
    }
    configure_logging(log_f, verbose=False)
    run_from_config(cfg)
    names = [p.name for p in dst.glob("*.tgz")]
    assert len(names) == 1
    assert re.fullmatch(r"arch_\d{14}\.tgz", names[0])
    assert (dst / ".done").is_file()


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_run_from_config_two_targets_update(tmp_path: Path):
    src = tmp_path / "src" / "one"
    src.mkdir(parents=True)
    (src / "f").write_text("x", encoding="utf-8")
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    flag_base = tmp_path / "flagbase"
    flag_base.mkdir(parents=True)
    log_f = tmp_path / "two.log"
    cfg = {
        "target": str(flag_base),
        "default_mode": "update",
        "sources": [{"path": str(src), "name": "one", "targets": [str(d1), str(d2)]}],
        "success_flag": ".synced",
        "log_file": str(log_f),
    }
    configure_logging(log_f, verbose=False)
    run_from_config(cfg)
    assert (d1 / "one" / "f").read_text() == "x"
    assert (d2 / "one" / "f").read_text() == "x"
    assert (flag_base / ".synced").is_file()


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_run_from_config_tgz_rotate(tmp_path: Path):
    src = tmp_path / "src" / "box"
    src.mkdir(parents=True)
    (src / "a.txt").write_text("v", encoding="utf-8")
    dst = tmp_path / "arch"
    dst.mkdir(parents=True)
    for i in range(4):
        p = dst / f"box-2026010{i}_120000.tgz"
        p.write_bytes(b"old")
        os.utime(p, (100 + i, 100 + i))
    log_f = tmp_path / "rot.log"
    flag_base = tmp_path / "fb"
    flag_base.mkdir(parents=True)
    cfg = {
        "target": str(flag_base),
        "default_mode": "tgz",
        "sources": [
            {
                "path": str(src),
                "name": "box",
                "target": str(dst),
                "rotate": True,
                "max_count": 3,
            }
        ],
        "success_flag": ".ok",
        "log_file": str(log_f),
    }
    configure_logging(log_f, verbose=False)
    run_from_config(cfg)
    tgzs = sorted(dst.glob("box-*.tgz"))
    assert len(tgzs) == 3
    assert (flag_base / ".ok").is_file()


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
@pytest.mark.skipif(not shutil.which("tar"), reason="tar not installed")
def test_run_from_config_tgz_symlink_source_archives_target_dir(tmp_path: Path):
    real = tmp_path / "real_data"
    real.mkdir()
    (real / "leaf.txt").write_text("inside-real", encoding="utf-8")
    link = tmp_path / "via_link"
    link.symlink_to(real, target_is_directory=True)
    dst = tmp_path / "out"
    log_f = tmp_path / "sym.log"
    flag_base = tmp_path / "fb"
    flag_base.mkdir()
    cfg = {
        "target": str(flag_base),
        "default_mode": "tgz",
        "sources": [{"path": str(link), "name": "pack", "target": str(dst)}],
        "success_flag": ".ok",
        "log_file": str(log_f),
    }
    configure_logging(log_f, verbose=False)
    run_from_config(cfg)
    tgzs = list(dst.glob("pack-*.tgz"))
    assert len(tgzs) == 1
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    subprocess.run(
        ["tar", "-xzf", str(tgzs[0]), "-C", str(extracted)],
        check=True,
        capture_output=True,
    )
    assert (extracted / "real_data" / "leaf.txt").read_text() == "inside-real"
    assert (flag_base / ".ok").is_file()


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_run_from_config_targets_per_entry_mode(tmp_path: Path):
    src = tmp_path / "src" / "data"
    src.mkdir(parents=True)
    (src / "file.txt").write_text("z", encoding="utf-8")
    d_upd = tmp_path / "mirror"
    d_copy = tmp_path / "snap"
    d_tgz = tmp_path / "archives"
    flag_base = tmp_path / "flagbase"
    flag_base.mkdir(parents=True)
    log_f = tmp_path / "per-target-mode.log"
    cfg = {
        "target": str(flag_base),
        "default_mode": "update",
        "sources": [
            {
                "path": str(src),
                "name": "data",
                "mode": "update",
                "targets": [
                    str(d_upd),
                    {"target": str(d_copy), "mode": "copy"},
                    {"target": str(d_tgz), "mode": "tgz"},
                ],
            }
        ],
        "success_flag": ".ok",
        "log_file": str(log_f),
    }
    configure_logging(log_f, verbose=False)
    run_from_config(cfg)
    assert (d_upd / "data" / "file.txt").read_text() == "z"
    copies = [p for p in d_copy.iterdir() if p.is_dir() and p.name.startswith("data-")]
    assert len(copies) == 1
    assert (copies[0] / "file.txt").read_text() == "z"
    tgzs = list(d_tgz.glob("data-*.tgz"))
    assert len(tgzs) == 1
    assert (flag_base / ".ok").is_file()


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_cli_subprocess_repo_root(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    src = tmp_path / "s" / "d"
    src.mkdir(parents=True)
    (src / "x").write_text("1", encoding="utf-8")
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "target": str(tmp_path / "out"),
                "default_mode": "update",
                "sources": [str(src)],
                "log_file": False,
            }
        ),
        encoding="utf-8",
    )
    script = repo / "backup-projects"
    r = subprocess.run(
        [str(script), "-c", str(cfg_path), "-q"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert (tmp_path / "out" / "d" / "x").read_text() == "1"


def test_module_main():
    repo = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, "-m", "backup_projects", "-h"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(repo)},
        check=False,
    )
    assert r.returncode == 0
    assert "YAML" in r.stdout or "yaml" in r.stdout.lower() or "config" in r.stdout.lower()
