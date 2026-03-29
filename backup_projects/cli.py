"""
CLI: резервное копирование по YAML через rsync, с журналом операций.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("error: install PyYAML: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

LOG = logging.getLogger("backup_projects")

VALID_MODES = ("update", "copy", "tgz")


class BackupError(Exception):
    pass


def _validate_mode(label: str, mode: str) -> str:
    if mode not in VALID_MODES:
        raise BackupError(f"{label} must be one of: {', '.join(VALID_MODES)}")
    return mode


def default_mode_from_config(cfg: dict[str, Any]) -> str:
    raw = cfg.get("default_mode")
    if raw is None:
        raw = cfg.get("mode", "update")
    if not isinstance(raw, str):
        raise BackupError("default_mode (or legacy mode) must be a string")
    return _validate_mode("default_mode", raw)


def normalize_sources(raw: Any, default_mode: str) -> list[tuple[str, str, str]]:
    """Возвращает список (path, name, mode) для каждого источника."""
    _validate_mode("default_mode", default_mode)
    out: list[tuple[str, str, str]] = []
    if not isinstance(raw, list):
        raise BackupError("sources must be a list")
    for item in raw:
        if isinstance(item, str):
            p = item
            name = Path(p).name
            out.append((p, name, default_mode))
        elif isinstance(item, dict):
            p = item.get("path")
            if not p:
                raise BackupError("each source object needs 'path'")
            name = item.get("name") or Path(str(p)).name
            m = item.get("mode", default_mode)
            if not isinstance(m, str):
                raise BackupError("source mode must be a string")
            out.append((str(p), str(name), _validate_mode("source mode", m)))
        else:
            raise BackupError("sources entries must be strings or objects with path")
    return out


def _rsync_base() -> list[str]:
    if not shutil.which("rsync"):
        raise BackupError("rsync not found; install with your distro package manager")
    return ["rsync", "-aH"]


def parse_rsync_extra(cfg: dict[str, Any]) -> list[str]:
    extra = cfg.get("rsync_extra")
    if extra is None:
        return []
    if isinstance(extra, str):
        return extra.split()
    if isinstance(extra, list) and all(isinstance(x, str) for x in extra):
        return list(extra)
    raise BackupError("rsync_extra must be a string or list of strings")


def _run(cmd: list[str]) -> None:
    LOG.debug("run: %s", " ".join(cmd))
    r = subprocess.run(cmd, check=False)
    if r.returncode != 0:
        raise BackupError(f"command failed ({r.returncode}): {' '.join(cmd)}")


def _is_remote(dest: str) -> bool:
    return ":" in dest and not dest.startswith("/")


def _local_target_root(target: str) -> Path:
    if _is_remote(target):
        raise BackupError("internal: local path expected")
    return Path(target).expanduser().resolve()


def _write_success_flag_local(flag_path: Path) -> None:
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = flag_path.with_suffix(flag_path.suffix + ".tmp")
    tmp.write_text(datetime.now().isoformat(timespec="seconds") + "\n", encoding="utf-8")
    tmp.replace(flag_path)
    LOG.info("success flag (local): %s", flag_path)


def _write_success_flag_remote(target: str, rel_flag: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".flag", delete=False, encoding="utf-8") as f:
        f.write(datetime.now().isoformat(timespec="seconds") + "\n")
        local_tmp = f.name
    try:
        remote_dir = target.rsplit(":", 1)[-1].rstrip("/") or "."
        remote_flag = f"{remote_dir.rstrip('/')}/{rel_flag}".replace("//", "/")
        host_part = target.rsplit(":", 1)[0] + ":"
        remote_spec = host_part + remote_flag
        cmd = _rsync_base() + [local_tmp, remote_spec]
        LOG.info("uploading success flag to remote: %s", remote_spec)
        _run(cmd)
    finally:
        try:
            os.unlink(local_tmp)
        except OSError:
            pass


def mode_update(
    sources: list[tuple[str, str]],
    target: str,
    sync_delete: bool,
    rsync_extra: list[str],
) -> None:
    base = _rsync_base() + rsync_extra
    if sync_delete:
        base = base + ["--delete"]
    for src, name in sources:
        src_path = Path(src).expanduser()
        if not src_path.is_dir():
            raise BackupError(f"source is not a directory: {src_path}")
        if _is_remote(target):
            dest = target.rstrip("/") + "/" + name + "/"
        else:
            root = _local_target_root(target)
            root.mkdir(parents=True, exist_ok=True)
            dest = str(root / name) + "/"
        cmd = base + [str(src_path) + "/", dest]
        LOG.info("mode=update rsync %s -> %s", src_path, dest)
        _run(cmd)


def mode_copy(
    sources: list[tuple[str, str]],
    target: str,
    rsync_extra: list[str],
) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = _rsync_base() + rsync_extra
    for src, name in sources:
        src_path = Path(src).expanduser()
        if not src_path.is_dir():
            raise BackupError(f"source is not a directory: {src_path}")
        dest_name = f"{name}-{stamp}"
        if _is_remote(target):
            dest = target.rstrip("/") + "/" + dest_name + "/"
        else:
            root = _local_target_root(target)
            root.mkdir(parents=True, exist_ok=True)
            dest = str(root / dest_name) + "/"
        cmd = base + [str(src_path) + "/", dest]
        LOG.info("mode=copy rsync %s -> %s", src_path, dest)
        _run(cmd)


def mode_tgz(
    sources: list[tuple[str, str]],
    target: str,
    rsync_extra: list[str],
) -> None:
    base = _rsync_base() + rsync_extra
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmpdir = tempfile.mkdtemp(prefix="backup-tgz-")
    LOG.debug("tgz temp dir: %s", tmpdir)
    try:
        archives: list[Path] = []
        for src, name in sources:
            src_path = Path(src).expanduser()
            if not src_path.is_dir():
                raise BackupError(f"source is not a directory: {src_path}")
            arc = Path(tmpdir) / f"{name}-{stamp}.tgz"
            tar_cmd = ["tar", "-czf", str(arc), "-C", str(src_path.parent), src_path.name]
            LOG.debug("run: %s", " ".join(tar_cmd))
            r = subprocess.run(tar_cmd, check=False)
            if r.returncode != 0:
                raise BackupError(f"tar failed ({r.returncode}): {' '.join(tar_cmd)}")
            archives.append(arc)
            LOG.info("created archive: %s", arc)
        if _is_remote(target):
            for arc in archives:
                cmd = base + [str(arc), target.rstrip("/") + "/"]
                LOG.info("uploading archive -> %s", target)
                _run(cmd)
        else:
            root = _local_target_root(target)
            root.mkdir(parents=True, exist_ok=True)
            for arc in archives:
                cmd = base + [str(arc), str(root) + "/"]
                LOG.info("rsync archive -> %s", root)
                _run(cmd)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def place_success_flag(cfg: dict[str, Any], target: str) -> None:
    rel = str(cfg.get("success_flag", ".backup-success")).lstrip("/")
    if _is_remote(target):
        _write_success_flag_remote(target, rel)
    else:
        root = _local_target_root(target)
        _write_success_flag_local(root / rel)


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BackupError(f"config not found: {path}")
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise BackupError("config root must be a mapping")
    return data


def _state_log_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME", "")
    if base:
        return Path(base) / "backup-projects"
    return Path.home() / ".local" / "state" / "backup-projects"


def _default_state_log_path(cfg: dict[str, Any]) -> Path:
    raw_name = cfg.get("log_filename", "backup.log")
    if raw_name is not None and not isinstance(raw_name, str):
        raise BackupError("log_filename must be a string")
    name = (raw_name or "backup.log").strip()
    if not name:
        raise BackupError("log_filename must be a non-empty filename")
    name = Path(name).name
    if not name:
        raise BackupError("log_filename must be a non-empty filename")
    return _state_log_dir() / name


def configure_logging(
    log_file: Path | None,
    *,
    verbose: bool,
) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    LOG.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    root = logging.getLogger("backup_projects")
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(level)
    stderr.setFormatter(fmt)
    root.addHandler(stderr)

    if log_file is not None:
        log_file = log_file.expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)
        LOG.info("log file: %s", log_file.resolve())


def run_from_config(cfg: dict[str, Any]) -> None:
    target = cfg.get("target")
    if not target or not isinstance(target, str):
        raise BackupError("target must be a non-empty string")

    dm = default_mode_from_config(cfg)
    sources = normalize_sources(cfg.get("sources"), dm)
    if not sources:
        raise BackupError("sources must be a non-empty list")

    rsync_extra = parse_rsync_extra(cfg)
    sync_delete = bool(cfg.get("sync_delete", False))

    LOG.info(
        "starting backup: default_mode=%s target=%s sources=%d",
        dm,
        target,
        len(sources),
    )

    for src, name, mode in sources:
        batch = [(src, name)]
        if mode == "update":
            mode_update(batch, target, sync_delete, rsync_extra)
        elif mode == "copy":
            mode_copy(batch, target, rsync_extra)
        else:
            mode_tgz(batch, target, rsync_extra)

    place_success_flag(cfg, target)
    LOG.info("backup finished successfully")


def resolve_log_path(cfg: dict[str, Any], cli_log: str | None) -> Path | None:
    if cli_log:
        return Path(cli_log).expanduser()

    if "log_file" in cfg and cfg["log_file"] is False:
        return None

    raw = cfg.get("log_file")

    if isinstance(raw, str):
        return Path(raw).expanduser()

    if raw is True or raw is None or "log_file" not in cfg:
        return _default_state_log_path(cfg)

    raise BackupError("log_file must be a string, true, false, or null")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backup directories via rsync from YAML config.")
    ap.add_argument(
        "-c",
        "--config",
        default="backup-config.yaml",
        help="path to YAML config (default: ./backup-config.yaml)",
    )
    ap.add_argument(
        "-l",
        "--log-file",
        default=None,
        help="append detailed log to this file (overrides log_file and log_filename)",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="debug messages on stderr",
    )
    ap.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="only warnings and errors on stderr",
    )
    args = ap.parse_args()

    try:
        cfg_path = Path(args.config)
        cfg = load_config(cfg_path)
        log_path = resolve_log_path(cfg, args.log_file)
        stderr_level = logging.WARNING if args.quiet else (logging.DEBUG if args.verbose else logging.INFO)
        configure_logging(log_path, verbose=args.verbose)
        for h in logging.getLogger("backup_projects").handlers:
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr:
                h.setLevel(stderr_level)
        run_from_config(cfg)
    except BackupError as e:
        logging.getLogger("backup_projects").error("%s", e)
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    print("ok: backup finished, success flag written")


if __name__ == "__main__":
    main()
