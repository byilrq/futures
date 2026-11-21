"""Consistent data-only SQLite backup/restore, with local and rclone rotation.

No archive extraction or program-file replacement occurs during restore.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from config import APPLICATION_ID, BASE_DIR, SCHEMA_VERSION, SYSTEM_DEFAULTS, now_shanghai, stamp
from storage import Store, dumps, loads

# New backups have a DATE-ONLY folder name. Continue accepting legacy names
# so a previous archive can always be restored after a code-only upgrade.
DATE_NAME = re.compile(r"^\d{8}$")
LEGACY_NAME = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{6}$")
NAME = re.compile(r"^(?:\d{8}|\d{8}_\d{6}_[0-9a-f]{6})$")
STAGE_ORPHAN_AGE_SECONDS = 24 * 3600
STAGE_NAME = re.compile(r"^\.futures-stage-[A-Za-z0-9_-]+$")
RESTORE_STAGE_NAME = re.compile(r"^\.restore-stage-[A-Za-z0-9_-]+$")
TEMP_TEXT_NAME = re.compile(r"^\.futures-tmp-[A-Za-z0-9_-]+$")
PREVIOUS_NAME = re.compile(r"^\.futures-previous-(\d{8})-[a-f0-9]{8}$")
RESCUE_NAME = re.compile(r"^before-restore-\d{8}_\d{6}_[a-f0-9]{6}\.sqlite3$")


def _valid_name(name):
    if not NAME.fullmatch(name):
        return False
    try:
        datetime.strptime(name[:8], "%Y%m%d")
        return True
    except ValueError:
        return False


def _sort_name(name):
    # New date-only backups sort ahead of legacy timestamped backups on that day.
    return name[:8], bool(DATE_NAME.fullmatch(name)), name


def _is_valid_snapshot(path):
    try:
        inspect_backup(path)
        return True
    except (OSError, ValueError, TypeError, sqlite3.DatabaseError):
        return False


def _owned_backup(folder):
    """A dated backup is ours only when both metadata and the DB verify."""
    marker = folder / "backup_meta.json"
    if folder.is_symlink() or not marker.is_file() or marker.is_symlink():
        return False
    try:
        if json.loads(marker.read_text(encoding="utf-8")).get("application") != "futures":
            return False
        inspect_backup(folder)
        return True
    except (OSError, ValueError, sqlite3.DatabaseError, TypeError):
        return False


def _recover_interrupted_publish(folder):
    """A crash after renaming YYYYMMDD aside must not lose its last good copy.

    Run only under backup.lock, never during another publisher's rename window.
    For a missing date, restore the newest *validated* managed previous tree.
    Foreign directories and symlinks are never removed or followed.
    """
    folder = Path(folder)
    if not folder.is_dir() or folder.is_symlink():
        return []
    grouped = {}
    for child in folder.iterdir():
        match = PREVIOUS_NAME.fullmatch(child.name)
        if match and child.is_dir() and not child.is_symlink():
            grouped.setdefault(match.group(1), []).append(child)
    recovered = []
    for date, candidates in grouped.items():
        destination = folder / date
        if destination.exists() or destination.is_symlink():
            continue  # Never overwrite an existing destination, even if invalid.
        for old in sorted(candidates, key=lambda p: p.stat().st_mtime_ns, reverse=True):
            if _owned_backup(old):
                os.replace(old, destination)
                recovered.append(date)
                break
    return recovered


def _cleanup_abandoned_staging(store, *, external=None, age_seconds=STAGE_ORPHAN_AGE_SECONDS):
    """Only touch our hidden temporary trees that are older than 24 hours.

    Caller MUST hold the exclusive data/backup.lock. A manually invoked cleanup
    cannot delete an active backup or restore, regardless of its age.
    """
    cutoff = time.time() - age_seconds
    removed, recovered = [], []
    roots = [(store.root / "backup", STAGE_NAME), (store.data, RESTORE_STAGE_NAME)]
    if external is not None:
        mirror = Path(external)
        if mirror.resolve() != (store.root / "backup").resolve():
            roots.append((mirror, STAGE_NAME))
    for root, pattern in roots:
        if root.is_symlink() or not root.is_dir():
            continue
        recovered.extend(_recover_interrupted_publish(root) if pattern is STAGE_NAME else [])
        for child in root.iterdir():
            try:
                info = child.lstat()
            except FileNotFoundError:
                continue
            if info.st_mtime > cutoff:
                continue
            if (pattern is STAGE_NAME and TEMP_TEXT_NAME.fullmatch(child.name)
                    and stat.S_ISREG(info.st_mode)):
                try:
                    child.unlink()
                    removed.append(str(child))
                except OSError:
                    pass
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            eligible = pattern.fullmatch(child.name)
            previous = PREVIOUS_NAME.fullmatch(child.name)
            if not eligible and not previous:
                continue
            if previous:
                # Do not erase the only still-usable backup after a crash.
                final = root / previous.group(1)
                if not _owned_backup(final) or not _owned_backup(child):
                    continue
            try:
                shutil.rmtree(child)
                removed.append(str(child))
            except OSError:
                # Cleanup errors must never invalidate an already good backup.
                pass
    return {"removed": removed, "recovered_dates": sorted(set(recovered))}


def cleanup_staging(store, external=None):
    """Manual diagnostics and cleanup, serialized with backup and restore."""
    lock = (store.data / "backup.lock").open("a+")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "detail": "备份或恢复正在进行，已跳过清理", "removed": []}
        if external is None:
            with store.read() as con:
                cfg = {**SYSTEM_DEFAULTS, **store.setting(con, "system", {})}
            target = str(cfg.get("backup_target") or "")
            if target.startswith("/"):
                mirror = Path(target)
                external = mirror if mirror.name.lower() == "futures" else mirror / "futures"
        return {"status": "ok", **_cleanup_abandoned_staging(store, external=external)}
    finally:
        lock.close()



def inspect_backup(path):
    path = Path(path)
    if path.is_dir():
        path /= "runtime.sqlite3"
    if not path.is_file() or path.stat().st_size < 4096:
        raise ValueError("未找到有效 runtime.sqlite3 数据备份")
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as con:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("备份数据库完整性检查未通过")
        if con.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("备份不属于 Futures，拒绝覆盖")
        version = con.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if not version or int(version[0]) != SCHEMA_VERSION:
            raise ValueError("备份数据库版本与当前程序不兼容")
        required = {"symbols", "settings", "snapshots", "events", "outbox", "requests", "price_history", "source_quotes", "jobs", "backtests"}
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables:
            raise ValueError("备份数据库缺少必需表")
        # Parse authoritative JSON before touching the active database.
        for row in con.execute("SELECT value FROM settings"):
            json.loads(row[0])
        for row in con.execute("SELECT contract_json,config_json,state_json FROM symbols"):
            for value in row:
                json.loads(value)
        if con.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("备份数据引用检查失败")
        return {"path": str(path), "symbols": con.execute("SELECT COUNT(*) FROM symbols WHERE active=1").fetchone()[0],
                "events": con.execute("SELECT COUNT(*) FROM events").fetchone()[0], "size": path.stat().st_size}


def list_backups(store):
    folder = store.root / "backup"
    if not folder.is_dir():
        return []
    # Repair a crash between moving the old YYYYMMDD aside and publishing the
    # replacement. Refuse to interfere if another backup currently holds lock.
    lock = (store.data / "backup.lock").open("a+")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            _recover_interrupted_publish(folder)
    finally:
        lock.close()
    result = []
    for item in folder.iterdir():
        if not _valid_name(item.name) or not item.is_dir() or item.is_symlink():
            continue
        db = item / "runtime.sqlite3"
        if not db.is_file() or db.is_symlink():
            continue
        meta_file = item / "backup_meta.json"
        try:
            meta = json.loads(meta_file.read_text()) if meta_file.is_file() else {}
        except (ValueError, OSError):
            meta = {}
        result.append({"name": item.name, "path": str(item), "size": db.stat().st_size,
                       "time": meta.get("time") or datetime.fromtimestamp(item.stat().st_mtime).isoformat()})
    result.sort(key=lambda item: _sort_name(item["name"]), reverse=True)
    return result


def _atomic_text(path, value):
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix=".futures-tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _copy_database(source, destination):
    # SQLite online backup includes committed WAL pages without copying a live WAL.
    src = sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(destination, timeout=30)
    try:
        src.backup(dst, pages=256, sleep=0.01)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.commit()
    finally:
        dst.close(); src.close()
    os.chmod(destination, 0o600)
    with open(destination, "rb") as file:
        os.fsync(file.fileno())
    inspect_backup(destination)


def _copy_frozen_snapshot(source, destination):
    """Restore from one verified frozen file, including on rclone FUSE mounts.

    Never run SQLite Backup API against a mounted remote snapshot: streaming
    its bytes to a private same-disk file makes it safe to inspect and mutate.
    """
    source, destination = Path(source), Path(destination)
    with source.open("rb") as inp, destination.open("xb") as out:
        os.chmod(destination, 0o600)
        shutil.copyfileobj(inp, out, length=4 * 1024 * 1024)
        out.flush()
        os.fsync(out.fileno())
    inspect_backup(destination)


def _rclone(*args):
    if not shutil.which("rclone"):
        raise ValueError("尚未安装或配置 rclone，已保留本地备份")
    try:
        proc = subprocess.run(["rclone", *map(str, args), "--contimeout", "15s", "--timeout", "45s", "--retries", "1"],
                              capture_output=True, text=True, timeout=240, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("rclone 操作超时，本地备份已保留") from exc
    if proc.returncode:
        # rclone logs can contain remote credentials or URLs; show a bounded generic error.
        raise ValueError(f"rclone 返回错误 {proc.returncode}；请在服务器核查 remote 连接，本地备份已保留")
    return proc.stdout


def _remote_root(target):
    cleaned = target.rstrip("/")
    # rclone:futures and rclone:folder/futures refer to the exact target.
    if cleaned.endswith(":"):
        return cleaned + "futures"
    if cleaned.rsplit("/", 1)[-1].lower() == "futures" or cleaned.split(":")[-1].lower() == "futures":
        return cleaned
    return cleaned + "/futures"


def _prune_directory(folder, keep_count=None, cutoff=None, exclude=None):
    owned = [p for p in Path(folder).iterdir() if _valid_name(p.name) and p.is_dir() and not p.is_symlink()]
    owned.sort(key=lambda p: _sort_name(p.name), reverse=True)
    kept = 0
    for path in owned:
        if path.name == exclude:
            kept += 1
            continue
        expired = keep_count is not None and kept >= keep_count
        expired = expired or (cutoff is not None and path.name[:8] < cutoff)
        # Never delete foreign content under an externally mounted directory.
        if expired and (path / "backup_meta.json").is_file():
            try:
                meta = json.loads((path / "backup_meta.json").read_text())
            except (ValueError, OSError):
                continue
            if meta.get("application") == "futures":
                shutil.rmtree(path)
        else:
            kept += 1


def _prune_rescue_databases(store, keep=1):
    """Retain only the latest recovery-before-restore copy after success."""
    files = []
    for item in store.data.iterdir():
        if not RESCUE_NAME.fullmatch(item.name) or item.is_symlink() or not item.is_file():
            continue
        files.append(item)
    files.sort(key=lambda item: item.name, reverse=True)
    for item in files[keep:]:
        try:
            # Never delete a foreign/corrupt file merely because of its name.
            inspect_backup(item)
            item.unlink()
        except (OSError, sqlite3.DatabaseError, ValueError):
            pass


def _configured_external(store):
    with store.read() as con:
        cfg = {**SYSTEM_DEFAULTS, **store.setting(con, "system", {})}
    target = str(cfg.get("backup_target") or "").strip()
    if not target:
        return None
    if ":" in target and not target.startswith("/"):
        return _remote_root(target)
    parent = Path(target)
    external = parent if parent.name.lower() == "futures" else parent / "futures"
    if (external == Path("/mnt/rclone") or Path("/mnt/rclone") in external.parents):
        if not os.path.ismount("/mnt/rclone"):
            raise ValueError("/mnt/rclone 尚未挂载，拒绝读取可能是本机旧数据的外部备份")
    return external


def _publish_dated_directory(staged, destination):
    """Replace a same-day snapshot only after validating the new backup.

    Previous dated directory is kept until new directory is published; on a
    rename error it is restored. The hidden rescue directory is deliberately
    not processed by ordinary date-based rotation.
    """
    staged, destination = Path(staged), Path(destination)
    inspect_backup(staged)
    previous = None
    if destination.exists() or destination.is_symlink():
        marker = destination / "backup_meta.json"
        if destination.is_symlink() or not destination.is_dir() or not marker.is_file():
            raise ValueError("当天备份目录已有非本程序管理的内容，拒绝覆盖")
        try:
            owned = json.loads(marker.read_text()).get("application") == "futures"
        except (ValueError, OSError):
            owned = False
        if not owned:
            raise ValueError("当天备份目录不属于 Futures，拒绝覆盖")
        previous = destination.parent / (".futures-previous-" + destination.name + "-" + uuid.uuid4().hex[:8])
        os.replace(destination, previous)
    try:
        os.replace(staged, destination)
    except BaseException:
        if previous is not None and previous.exists() and not destination.exists():
            os.replace(previous, destination)
        raise
    if previous is not None:
        # A failed cleanup is non-fatal: the published YYYYMMDD is valid.
        try:
            shutil.rmtree(previous)
        except OSError:
            pass


def backup_data(store, target=None):
    with store.read() as con:
        cfg = {**SYSTEM_DEFAULTS, **store.setting(con, "system", {})}
    directory = store.root / "backup"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (store.data / "backup.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise ValueError("另一个备份任务正在执行") from exc
    name = now_shanghai().strftime("%Y%m%d")
    tempdir = None
    destination = directory / name
    result = None
    try:
        target = cfg["backup_target"] if target is None else target
        mirror = None
        external_ready = False
        if target and target.startswith("/"):
            parent = Path(target)
            mirror = parent if parent.name.lower() == "futures" else parent / "futures"
        # As in Quant r67, abandoned staging is removed on the next task,
        # never from /tmp; this lock protects active tasks of any age.
        _cleanup_abandoned_staging(store)
        with store.tx() as con:
            # Migrate the legacy status once, BEFORE marking the next attempt
            # running. A failed attempt must never replace the last good time.
            old_status = store.meta(con, "backup_status", {})
            prior_success = store.meta(con, "backup_last_success", {})
            if not prior_success and isinstance(old_status, dict) and old_status.get("status") == "ok" and old_status.get("time"):
                store.set_meta(con, "backup_last_success", {"time": old_status["time"], "path": old_status.get("path", "")})
            store.set_meta(con, "backup_status", {"status": "running", "time": stamp(), "detail": "正在备份运行数据"})
        tempdir = Path(tempfile.mkdtemp(prefix=".futures-stage-", dir=directory))
        _copy_database(store.path, tempdir / "runtime.sqlite3")
        meta = {"application": "futures", "schema_version": SCHEMA_VERSION, "time": stamp(), "name": name,
                "required": ["runtime.sqlite3"], "scope": "运行数据、设置、登录账户、快照、交易、推送记录和回测；不含代码"}
        _atomic_text(tempdir / "backup_meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        _publish_dated_directory(tempdir, destination)
        _atomic_text(directory / "LATEST", name + "\n")
        # Versions before this update accumulated full pre-restore SQLite
        # copies. Keep the most recent rescue, not an unbounded archive.
        _prune_rescue_databases(store, keep=1)
        # Local retention must succeed even when the remote target fails. Old
        # daily local copies must not grow indefinitely on rclone outages.
        _prune_directory(directory, keep_count=cfg["local_keep"], exclude=name)
        remote_path = ""
        if target:
            remote_path = _remote_root(target)
            cutoff = (now_shanghai().date() - timedelta(days=cfg["backup_keep_days"] - 1)).strftime("%Y%m%d")
            if ":" in target and not target.startswith("/"):
                _rclone("copy", destination, remote_path + "/" + name)
                _rclone("check", destination, remote_path + "/" + name, "--one-way", "--size-only")
                _rclone("copyto", directory / "LATEST", remote_path + "/LATEST")
                listings = json.loads(_rclone("lsjson", remote_path, "--dirs-only"))
                for item in listings:
                    candidate = item.get("Name", "")
                    if _valid_name(candidate) and candidate[:8] < cutoff and candidate != name:
                        # Require the ownership marker before removing an expired directory.
                        marker = json.loads(_rclone("cat", remote_path + "/" + candidate + "/backup_meta.json"))
                        if marker.get("application") == "futures":
                            _rclone("purge", remote_path + "/" + candidate)
            else:
                parent = Path(target)
                mirror = parent if parent.name.lower() == "futures" else parent / "futures"
                if not parent.is_dir():
                    raise ValueError("外部备份路径不存在/挂载未就绪，本地备份已保留")
                # /mnt/rclone is explicitly a mount-backed target. Do not
                # silently fill the VPS root disk after rclone FUSE disconnects.
                if parent == Path("/mnt/rclone") or Path("/mnt/rclone") in parent.parents:
                    if not os.path.ismount("/mnt/rclone"):
                        raise ValueError("/mnt/rclone 挂载未就绪，本地备份已保留；拒绝写到未挂载的目录")
                if mirror.is_symlink():
                    raise ValueError("外部备份目录是符号链接，拒绝写入；本地备份已保留")
                if mirror.resolve() == directory.resolve() or directory.resolve() in mirror.resolve().parents:
                    raise ValueError("外部备份路径不能位于本地 backup 内")
                mirror.mkdir(mode=0o700, exist_ok=True)
                external_ready = True
                _cleanup_abandoned_staging(store, external=mirror)
                staged = Path(tempfile.mkdtemp(prefix=".futures-stage-", dir=mirror))
                try:
                    for file in destination.iterdir():
                        shutil.copy2(file, staged / file.name)
                    inspect_backup(staged)
                    _publish_dated_directory(staged, mirror / name)
                    _atomic_text(mirror / "LATEST", name + "\n")
                finally:
                    if staged.exists():
                        shutil.rmtree(staged)
                _prune_directory(mirror, cutoff=cutoff, exclude=name)
        result = {"status": "ok", "time": stamp(), "path": str(destination), "name": name,
                  "detail": "本地备份完成" + ("，外部备份完成" if target else ""), "target": remote_path}
        with store.tx() as con:
            # Commit both in the same transaction; never advance the success
            # watermark when the local or configured external backup fails.
            store.set_meta(con, "backup_last_success", {"time": result["time"], "path": result["path"], "name": name})
            store.set_meta(con, "backup_status", result)
        return result
    except Exception as exc:
        with store.tx() as con:
            store.set_meta(con, "backup_status", {"status": "error", "time": stamp(), "detail": str(exc)[:600],
                                                  "path": str(destination) if destination.exists() else ""})
        raise
    finally:
        if tempdir is not None and tempdir.exists():
            try:
                shutil.rmtree(tempdir)
            except OSError:
                # Preserve an already validated backup; next task will retry
                # abandoned temp cleanup after the 24-hour safety window.
                pass
        try:
            _cleanup_abandoned_staging(store, external=mirror if locals().get("external_ready") else None)
        except OSError:
            pass
        lock.close()


def resolve_restore_source(store, source, staging):
    source = str(source or "latest").strip()
    if source == "latest":
        # Prefer the newest *valid* local copy, ignoring corrupt/partial days.
        # If local backup is empty, try the configured mirror's LATEST.
        entries = list_backups(store)
        source = ""
        for item in entries:
            try:
                inspect_backup(item["path"])
            except (OSError, ValueError, sqlite3.DatabaseError):
                continue
            source = item["path"]
            break
        if not source:
            external = _configured_external(store)
            if external is None:
                raise ValueError("没有有效的本地日期备份，请输入外部备份路径或 rclone remote")
            if isinstance(external, Path):
                listing = sorted((x for x in external.iterdir() if _valid_name(x.name) and x.is_dir() and not x.is_symlink()),
                                 key=lambda x: _sort_name(x.name), reverse=True) if external.is_dir() else []
                for item in listing:
                    try:
                        inspect_backup(item)
                    except (OSError, ValueError, sqlite3.DatabaseError):
                        continue
                    source = str(item)
                    break
                if not source:
                    raise ValueError("本地与已配置的外部备份目录均没有有效运行数据")
            else:
                source = external + "/LATEST"
    elif DATE_NAME.fullmatch(source):
        if not _valid_name(source):
            raise ValueError("日期格式无效，请输入 YYYYMMDD，例如 20261108")
        exact = store.root / "backup" / source
        valid_local = ""
        candidates = ([{"path": str(exact)}] if exact.is_dir() and not exact.is_symlink() else [])
        candidates += [x for x in list_backups(store) if x["name"].startswith(source + "_")]
        for item in candidates:
            try:
                inspect_backup(item["path"])
                valid_local = item["path"]
                break
            except (OSError, ValueError, sqlite3.DatabaseError):
                continue
        if valid_local:
            source = valid_local
        else:
            requested_date = source
            external = _configured_external(store)
            if external is None:
                raise ValueError(f"没有找到 {requested_date} 日期的有效本地备份")
            if isinstance(external, Path):
                direct = external / source
                if direct.is_dir() and not direct.is_symlink() and _is_valid_snapshot(direct):
                    source = str(direct)
                else:
                    matches = [x for x in external.glob(source + "_*") if _valid_name(x.name) and x.is_dir() and not x.is_symlink() and _is_valid_snapshot(x)]
                    if not matches:
                        raise ValueError(f"本地和外部目录均没有 {requested_date} 日期的有效备份")
                    source = str(sorted(matches, key=lambda x: _sort_name(x.name), reverse=True)[0])
            else:
                source = external.rstrip("/") + "/" + source
    if re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9_./-]+", source):
        if ".." in source.split("/"):
            raise ValueError("remote 路径不能含上级目录")
        remote = source.rstrip("/")
        if remote.endswith("/LATEST"):
            name = _rclone("cat", remote).strip()
            if not _valid_name(name):
                raise ValueError("远端 LATEST 标记无效")
            remote = remote.rsplit("/", 1)[0] + "/" + name
        if not remote.endswith(".sqlite3"):
            remote += "/runtime.sqlite3"
        local = Path(staging) / "runtime.sqlite3"
        _rclone("copyto", remote, local)
        return local
    local = Path(source).expanduser().resolve()
    if local.is_dir():
        local /= "runtime.sqlite3"
    return local


def restore_data(store, source="latest", services_stopped=False):
    if not services_stopped:
        raise ValueError("恢复前必须停止 futures-web 与 futures-worker；请从 futures.sh 菜单重建")
    locks = []
    try:
        for name in ("worker.lock", "portal.lock", "backup.lock"):
            lock = (store.data / name).open("a+")
            locks.append(lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("服务或备份仍在运行，拒绝在线替换数据库") from exc
        _cleanup_abandoned_staging(store)
        with tempfile.TemporaryDirectory(prefix=".restore-stage-", dir=store.data) as stage:
            candidate = resolve_restore_source(store, source, stage)
            if candidate.resolve() == store.path.resolve():
                raise ValueError("不能将正在使用的数据文件作为恢复来源")
            needed = candidate.stat().st_size * 2 + store.path.stat().st_size + 16 * 1024 * 1024
            if shutil.disk_usage(store.data).free < needed:
                raise ValueError("磁盘空间不足，未覆盖当前数据；请清理空间后重试")
            staged = Path(stage) / "validated.sqlite3"
            if candidate.parent == Path(stage):
                # A downloaded remote snapshot is already a frozen, verified
                # standalone SQLite file. Rename in place, do not duplicate it.
                os.replace(candidate, staged)
                inspect_backup(staged)
            else:
                _copy_frozen_snapshot(candidate, staged)
            info = inspect_backup(staged)
            # Cancel historical notification/job queues; rotate sessions on full restore.
            with closing(sqlite3.connect(staged)) as con:
                con.execute("UPDATE outbox SET status='cancelled',detail='恢复历史备份后取消补发' WHERE status IN ('pending','sending','failed')")
                con.execute("UPDATE jobs SET status='error',result='恢复历史备份，旧任务已取消' WHERE status IN ('pending','running')")
                for row in con.execute("SELECT id,state_json FROM symbols").fetchall():
                    state = loads(row[1]); state["strategy"]["needs_baseline"] = True
                    state["monitor"] = {"anchor": None}
                    con.execute("UPDATE symbols SET state_json=?,revision=revision+1 WHERE id=?", (dumps(state), row[0]))
                con.execute("UPDATE metadata SET value=? WHERE key='secret_key'", (dumps(uuid.uuid4().hex + uuid.uuid4().hex),))
                Store.set_meta(con, "restore_status", {"time": stamp(), "source": str(source)})
                Store.set_meta(con, "heartbeat", {})
                Store.set_meta(con, "backup_status", {"status": "restored", "time": stamp(),
                                                      "detail": "数据已恢复，等待下一次备份生成"})
                con.commit()
            inspect_backup(staged)
            rescue = store.data / ("before-restore-" + now_shanghai().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6] + ".sqlite3")
            _copy_database(store.path, rescue)
            # Checkpoint only after verified staged/rescue files both exist.
            with closing(sqlite3.connect(store.path, timeout=15)) as con:
                checkpoint = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint[0]:
                    raise ValueError("数据库仍被占用，未覆盖当前数据")
            # Remove checkpointed WAL before swapping the database. If the
            # process dies just after replace, a stale WAL cannot attach to it.
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(store.path) + suffix)
                if sidecar.exists():
                    sidecar.unlink()
            replaced = False
            try:
                os.replace(staged, store.path)
                replaced = True
                os.chmod(store.path, 0o600)
                inspect_backup(store.path)
            except Exception:
                if replaced:
                    # Rescue already contains a fully validated frozen copy
                    # of the pre-restore database, including its prior WAL.
                    os.replace(rescue, store.path)
                    os.chmod(store.path, 0o600)
                raise
            _prune_rescue_databases(store, keep=1)
            return {"status": "ok", "symbols": info["symbols"], "events": info["events"],
                    "rescue": str(rescue), "detail": "仅恢复运行数据库；程序文件未被覆盖，旧推送不会补发"}
    finally:
        for lock in reversed(locks):
            lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "list", "inspect", "restore", "cleanup-staging"))
    parser.add_argument("--root", default=str(BASE_DIR))
    parser.add_argument("--source", default="latest")
    parser.add_argument("--services-stopped", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    store = Store(args.root).initialize()
    if args.command == "backup":
        result = backup_data(store)
    elif args.command == "list":
        result = list_backups(store)
    elif args.command == "inspect":
        with tempfile.TemporaryDirectory(dir=store.data) as staging:
            result = inspect_backup(resolve_restore_source(store, args.source, staging))
    elif args.command == "cleanup-staging":
        result = cleanup_staging(store)
    else:
        if not args.confirm:
            parser.error("恢复需要 --confirm；菜单中默认不确认")
        result = restore_data(store, args.source, args.services_stopped)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
