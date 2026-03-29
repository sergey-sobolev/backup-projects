import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from backup_projects.cli import (
    BackupError,
    _normalize_sources,
    configure_logging,
    load_config,
    parse_rsync_extra,
    resolve_log_path,
    run_from_config,
)


def test_normalize_sources_strings():
    raw = ["/a/b/foo", "/c/d"]
    assert _normalize_sources(raw) == [("/a/b/foo", "foo"), ("/c/d", "d")]


def test_normalize_sources_objects():
    raw = [{"path": "/x/y", "name": "custom"}, {"path": "/z"}]
    assert _normalize_sources(raw) == [("/x/y", "custom"), ("/z", "z")]


def test_normalize_sources_invalid_type():
    with pytest.raises(BackupError, match="sources must be a list"):
        _normalize_sources("not-a-list")


def test_normalize_sources_bad_entry():
    with pytest.raises(BackupError, match="sources entries"):
        _normalize_sources([123])


def test_parse_rsync_extra():
    assert parse_rsync_extra({}) == []
    assert parse_rsync_extra({"rsync_extra": "--exclude .git"}) == ["--exclude", ".git"]
    assert parse_rsync_extra({"rsync_extra": ["--exclude", ".cache"]}) == ["--exclude", ".cache"]


def test_parse_rsync_extra_invalid():
    with pytest.raises(BackupError, match="rsync_extra"):
        parse_rsync_extra({"rsync_extra": 1})


def test_load_config_roundtrip(tmp_path: Path):
    p = tmp_path / "c.yaml"
    data = {"target": "/tmp", "mode": "update", "sources": ["/a"], "log_file": False}
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_config(p) == data


def test_load_config_missing(tmp_path: Path):
    with pytest.raises(BackupError, match="config not found"):
        load_config(tmp_path / "nope.yaml")


def test_resolve_log_path_cli_overrides():
    cfg = {"log_file": False}
    assert resolve_log_path(cfg, "/tmp/x.log") == Path("/tmp/x.log")


def test_resolve_log_path_false():
    cfg = {"log_file": False}
    assert resolve_log_path(cfg, None) is None


def test_resolve_log_path_default_when_key_missing():
    cfg = {"target": "/x"}
    p = resolve_log_path(cfg, None)
    assert p is not None
    assert p.name == "backup.log"


@pytest.mark.skipif(not shutil.which("rsync"), reason="rsync not installed")
def test_run_from_config_update_local(tmp_path: Path):
    src = tmp_path / "src" / "proj"
    src.mkdir(parents=True)
    (src / "f.txt").write_text("data", encoding="utf-8")
    dst = tmp_path / "dst"
    log_f = tmp_path / "run.log"
    cfg = {
        "target": str(dst),
        "mode": "update",
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
                "mode": "update",
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
