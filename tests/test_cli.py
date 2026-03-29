import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from backup_projects.cli import (
    BackupError,
    configure_logging,
    default_mode_from_config,
    load_config,
    normalize_sources,
    parse_rsync_extra,
    resolve_log_path,
    run_from_config,
    tgz_datetime_suffix_enabled,
)

GT = "/default/backup"


def test_normalize_sources_strings():
    raw = ["/a/b/foo", "/c/d"]
    assert normalize_sources(raw, "update", GT) == [
        ("/a/b/foo", "foo", "update", [GT]),
        ("/c/d", "d", "update", [GT]),
    ]


def test_normalize_sources_objects():
    raw = [{"path": "/x/y", "name": "custom"}, {"path": "/z"}]
    assert normalize_sources(raw, "copy", GT) == [
        ("/x/y", "custom", "copy", [GT]),
        ("/z", "z", "copy", [GT]),
    ]


def test_normalize_sources_per_source_mode():
    raw = [
        {"path": "/a", "mode": "tgz"},
        "/b",
        {"path": "/c", "name": "see", "mode": "copy"},
    ]
    assert normalize_sources(raw, "update", GT) == [
        ("/a", "a", "tgz", [GT]),
        ("/b", "b", "update", [GT]),
        ("/c", "see", "copy", [GT]),
    ]


def test_normalize_sources_per_source_target():
    raw = [{"path": "/a", "target": "/mnt/usb"}]
    assert normalize_sources(raw, "update", GT) == [
        ("/a", "a", "update", ["/mnt/usb"]),
    ]


def test_normalize_sources_targets_list():
    raw = [{"path": "/a", "targets": ["/t1", "/t2"]}]
    assert normalize_sources(raw, "update", GT) == [
        ("/a", "a", "update", ["/t1", "/t2"]),
    ]


def test_normalize_sources_targets_precedence_over_target():
    raw = [{"path": "/a", "target": "/alone", "targets": ["/x", "/y"]}]
    assert normalize_sources(raw, "update", GT) == [
        ("/a", "a", "update", ["/x", "/y"]),
    ]


def test_normalize_sources_empty_targets_rejected():
    with pytest.raises(BackupError, match="targets must be a non-empty"):
        normalize_sources([{"path": "/a", "targets": []}], "update", GT)


def test_normalize_sources_invalid_global_target():
    with pytest.raises(BackupError, match="global target"):
        normalize_sources(["/a"], "update", "")


def test_normalize_sources_invalid_default_mode():
    with pytest.raises(BackupError, match="default_mode"):
        normalize_sources(["/a"], "nope", GT)


def test_normalize_sources_invalid_source_mode():
    with pytest.raises(BackupError, match="source mode"):
        normalize_sources([{"path": "/a", "mode": "bad"}], "update", GT)


def test_normalize_sources_invalid_type():
    with pytest.raises(BackupError, match="sources must be a list"):
        normalize_sources("not-a-list", "update", GT)


def test_normalize_sources_bad_entry():
    with pytest.raises(BackupError, match="sources entries"):
        normalize_sources([123], "update", GT)


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


def test_parse_rsync_extra_invalid():
    with pytest.raises(BackupError, match="rsync_extra"):
        parse_rsync_extra({"rsync_extra": 1})


def test_tgz_datetime_suffix_enabled():
    assert tgz_datetime_suffix_enabled({}) is False
    assert tgz_datetime_suffix_enabled({"tgz_datetime_suffix": True}) is True


def test_tgz_datetime_suffix_invalid_type():
    with pytest.raises(BackupError, match="tgz_datetime_suffix"):
        tgz_datetime_suffix_enabled({"tgz_datetime_suffix": "yes"})


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
