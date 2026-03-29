"""
CLI: резервное копирование по YAML через rsync, с журналом операций.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
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


def merge_tgz_rotate(
    cfg: dict[str, Any],
    source_dict: dict[str, Any] | None,
    target_dict: dict[str, Any] | None,
) -> tuple[bool, int | None]:
    """rotate/max_count: приоритет targets[] > источник > корень конфига."""
    r: Any = cfg["rotate"] if "rotate" in cfg else False
    mc: Any = cfg["max_count"] if "max_count" in cfg else None
    if source_dict is not None:
        if "rotate" in source_dict:
            r = source_dict["rotate"]
        if "max_count" in source_dict:
            mc = source_dict["max_count"]
    if target_dict is not None:
        if "rotate" in target_dict:
            r = target_dict["rotate"]
        if "max_count" in target_dict:
            mc = target_dict["max_count"]
    if not isinstance(r, bool):
        raise BackupError("rotate must be a boolean")
    if r:
        if not isinstance(mc, int) or isinstance(mc, bool) or mc < 1:
            raise BackupError("max_count must be a positive integer when rotate is true")
        return (True, mc)
    return (False, None)


def _targets_for_source_entry(
    item: dict[str, Any],
    global_target: str,
    source_mode: str,
) -> list[tuple[str, str, dict[str, Any] | None]]:
    """(цель, mode, dict элемента targets для merge_tgz_rotate или None)."""
    ts = item.get("targets")
    if ts is not None:
        if not isinstance(ts, list) or not ts:
            raise BackupError("targets must be a non-empty list")
        out: list[tuple[str, str, dict[str, Any] | None]] = []
        for x in ts:
            if isinstance(x, str):
                if not x.strip():
                    raise BackupError("each string in targets must be non-empty")
                out.append((x.strip(), source_mode, None))
            elif isinstance(x, dict):
                dest = x.get("target")
                if not dest or not isinstance(dest, str) or not dest.strip():
                    raise BackupError("each targets[] object needs non-empty 'target'")
                m = x.get("mode", source_mode)
                if not isinstance(m, str):
                    raise BackupError("targets[].mode must be a string")
                out.append((dest.strip(), _validate_mode("targets[].mode", m), x))
            else:
                raise BackupError("targets entries must be strings or objects with 'target'")
        return out
    one = item.get("target")
    if one is not None:
        if not isinstance(one, str) or not one.strip():
            raise BackupError("target must be a non-empty string")
        return [(one.strip(), source_mode, None)]
    return [(global_target, source_mode, None)]


def normalize_sources(
    raw: Any,
    default_mode: str,
    global_target: str,
    cfg: dict[str, Any],
) -> list[tuple[str, str, list[tuple[str, str, bool, int | None]]]]:
    """(path, name, [(destination, mode, rotate, max_count_or_none), ...])."""
    _validate_mode("default_mode", default_mode)
    if not isinstance(global_target, str) or not global_target.strip():
        raise BackupError("global target must be a non-empty string")
    gt = global_target.strip()
    out: list[tuple[str, str, list[tuple[str, str, bool, int | None]]]] = []
    if not isinstance(raw, list):
        raise BackupError("sources must be a list")
    for item in raw:
        if isinstance(item, str):
            p = item
            name = Path(p).name
            rot, mc = merge_tgz_rotate(cfg, None, None)
            out.append((p, name, [(gt, default_mode, rot, mc)]))
        elif isinstance(item, dict):
            if "enable" in item:
                ev = item["enable"]
                if not isinstance(ev, bool):
                    raise BackupError("source enable must be a boolean")
                if not ev:
                    continue
            p = item.get("path")
            if not p:
                raise BackupError("each source object needs 'path'")
            name = item.get("name") or Path(str(p)).name
            m = item.get("mode", default_mode)
            if not isinstance(m, str):
                raise BackupError("source mode must be a string")
            sm = _validate_mode("source mode", m)
            raw_jobs = _targets_for_source_entry(item, gt, sm)
            jobs = [
                (d, md, *merge_tgz_rotate(cfg, item, td)) for d, md, td in raw_jobs
            ]
            out.append((str(p), str(name), jobs))
        else:
            raise BackupError("sources entries must be strings or objects with path")
    return out


def _rsync_base() -> list[str]:
    if not shutil.which("rsync"):
        raise BackupError("rsync not found; install with your distro package manager")
    # --protect-args: пути с пробелами и спецсимволами на удалённой стороне не разбивает remote shell
    return ["rsync", "-aH", "--protect-args"]


def tgz_datetime_suffix_enabled(cfg: dict[str, Any]) -> bool:
    raw = cfg.get("tgz_datetime_suffix")
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise BackupError("tgz_datetime_suffix must be a boolean")
    return raw


def parse_rsync_extra(cfg: dict[str, Any]) -> list[str]:
    extra = cfg.get("rsync_extra")
    if extra is None:
        return []
    if isinstance(extra, str):
        return shlex.split(extra, posix=True)
    if isinstance(extra, list) and all(isinstance(x, str) for x in extra):
        return list(extra)
    raise BackupError("rsync_extra must be a string or list of strings")


def _run(cmd: list[str]) -> None:
    LOG.debug("run: %s", shlex.join(cmd))
    r = subprocess.run(cmd, check=False)
    if r.returncode != 0:
        raise BackupError(f"command failed ({r.returncode}): {shlex.join(cmd)}")


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


def _tgz_backups_for_name(root: Path, name: str) -> list[Path]:
    """Файлы .tgz для логического имени: name-YYYYMMDD_HHMMSS.tgz и name_YYYYMMDDHHMMSS.tgz."""
    esc = re.escape(name)
    legacy = re.compile(rf"^{esc}-\d{{8}}_\d{{6}}\.tgz$")
    compact = re.compile(rf"^{esc}_\d{{14}}\.tgz$")
    out: list[Path] = []
    if not root.is_dir():
        return out
    for p in root.iterdir():
        if p.is_file() and (legacy.match(p.name) or compact.match(p.name)):
            out.append(p)
    return out


def prune_tgz_archives(root: Path, name: str, max_count: int) -> None:
    """Оставляет не более max_count самых новых по mtime; остальные удаляет."""
    files = _tgz_backups_for_name(root, name)
    if len(files) <= max_count:
        return
    by_mtime = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
    for p in by_mtime[max_count:]:
        LOG.info("rotate: removing old archive %s", p)
        p.unlink()


def mode_tgz(
    sources: list[tuple[str, str]],
    target: str,
    rsync_extra: list[str],
    *,
    underscore_datetime_suffix: bool = False,
    rotate: bool = False,
    max_count: int | None = None,
) -> None:
    base = _rsync_base() + rsync_extra
    if underscore_datetime_suffix:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")

        def archive_basename(n: str) -> str:
            return f"{n}_{stamp}.tgz"
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        def archive_basename(n: str) -> str:
            return f"{n}-{stamp}.tgz"

    tmpdir = tempfile.mkdtemp(prefix="backup-tgz-")
    LOG.debug("tgz temp dir: %s", tmpdir)
    try:
        archives: list[Path] = []
        names: list[str] = []
        for src, name in sources:
            src_path = Path(src).expanduser()
            if not src_path.is_dir():
                raise BackupError(f"source is not a directory: {src_path}")
            arc = Path(tmpdir) / archive_basename(name)
            tar_cmd = ["tar", "-czf", str(arc), "-C", str(src_path.parent), src_path.name]
            LOG.debug("run: %s", shlex.join(tar_cmd))
            r = subprocess.run(tar_cmd, check=False)
            if r.returncode != 0:
                raise BackupError(f"tar failed ({r.returncode}): {shlex.join(tar_cmd)}")
            archives.append(arc)
            names.append(name)
            LOG.info("created archive: %s", arc)
        if _is_remote(target):
            if rotate and max_count is not None:
                LOG.warning(
                    "rotate/max_count ignored for remote target %s (local prune only)",
                    target,
                )
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
            if rotate and max_count is not None:
                for n in names:
                    prune_tgz_archives(root, n, max_count)
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


def max_workers_from_config(cfg: dict[str, Any], num_tasks: int) -> int:
    if num_tasks < 1:
        return 1
    raw = cfg.get("max_workers")
    if raw is None:
        return min(8, num_tasks)
    if type(raw) is not int or raw < 1:
        raise BackupError("max_workers must be a positive integer")
    return min(raw, num_tasks)


def _run_backup_job(
    src: str,
    name: str,
    tgt: str,
    mode: str,
    rot: bool,
    mc: int | None,
    sync_delete: bool,
    rsync_extra: list[str],
    tgz_dt_suffix: bool,
) -> None:
    batch = [(src, name)]
    LOG.info(
        "job: source %s name=%s mode=%s -> target %s rotate=%s max_count=%s",
        src,
        name,
        mode,
        tgt,
        rot,
        mc,
    )
    if mode == "update":
        mode_update(batch, tgt, sync_delete, rsync_extra)
    elif mode == "copy":
        mode_copy(batch, tgt, rsync_extra)
    else:
        mode_tgz(
            batch,
            tgt,
            rsync_extra,
            underscore_datetime_suffix=tgz_dt_suffix,
            rotate=rot,
            max_count=mc,
        )


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
    target = target.strip()

    dm = default_mode_from_config(cfg)
    sources = normalize_sources(cfg.get("sources"), dm, target, cfg)
    if not sources:
        raise BackupError("sources must be a non-empty list")

    rsync_extra = parse_rsync_extra(cfg)
    sync_delete = bool(cfg.get("sync_delete", False))
    tgz_dt_suffix = tgz_datetime_suffix_enabled(cfg)

    tasks: list[tuple[str, str, str, str, bool, int | None]] = []
    for src, name, jobs in sources:
        for tgt, mode, rot, mc in jobs:
            tasks.append((src, name, tgt, mode, rot, mc))

    workers = max_workers_from_config(cfg, len(tasks))
    LOG.info(
        "starting backup: default_mode=%s default_target=%s source_entries=%d jobs=%d max_workers=%d",
        dm,
        target,
        len(sources),
        len(tasks),
        workers,
    )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _run_backup_job,
                src,
                name,
                tgt,
                mode,
                rot,
                mc,
                sync_delete,
                rsync_extra,
                tgz_dt_suffix,
            )
            for src, name, tgt, mode, rot, mc in tasks
        ]
        for fut in futures:
            fut.result()

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
