"""Futures 本地管理命令；密码只从标准输入/终端读取，从不经命令行参数。

本模块不控制 systemd，服务启停由 futures.sh 负责。status/config/quote 不
写交易、策略或推送记录。unit 仅生成本项目的服务配置，供脚本审阅后安装。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

from werkzeug.security import generate_password_hash

from config import APPLICATION_ID, BASE_DIR, SYSTEM_DEFAULTS, now_shanghai, parse_timestamp, stamp, validate_system
from nginx_proxy import backend_web
from contracts import normalize_contract
from market_data import collect_quotes, is_fresh
from storage import Store


OWNER_MARKER = ".futures-owned"
PROTECTED_TOP = {"etc", "usr", "bin", "sbin", "lib", "lib64", "boot", "efi", "dev", "proc", "sys", "run"}
PROTECTED_COMPONENTS = {"quant", "nginx"}
SECRET_KEYS = {"NTFY_PASSWORD", "GOTIFY_TOKEN", "password_hash", "secret_key"}


def _no_symlink_components(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"路径不能包含符号链接：{component}")


def validate_project_root(value) -> Path:
    """Deployment path guard shared by CLI service rendering and deletion."""
    raw = os.fspath(value)
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", raw):
        raise ValueError("项目目录须为不含空格的绝对路径，仅允许英文、数字、点、下划线和短横线")
    path = Path(raw)
    if ".." in path.parts or raw != str(path):
        raise ValueError("项目目录不能包含上级目录或非规范路径")
    if len(path.parts) < 2 or path.parts[1] in PROTECTED_TOP:
        raise ValueError("不能使用受保护的系统目录")
    if any(part.lower() in PROTECTED_COMPONENTS for part in path.parts):
        raise ValueError("不能在 quant、nginx 或其子目录安装期货程序")
    if not re.fullmatch(r"futures(?:[-_][A-Za-z0-9_.-]+)?", path.name):
        raise ValueError("请使用独立的 futures 或 futures-名称 目录")
    _no_symlink_components(path)
    if path.exists() and not path.is_dir():
        raise ValueError("项目路径已存在且不是目录")
    for name in ("data", "backup", ".venv", OWNER_MARKER, "initial_credentials.txt"):
        if (path / name).is_symlink():
            raise ValueError(f"项目的 {name} 不能是符号链接")
    return path


def _store(root, initialize=False):
    root = validate_project_root(root)
    path = root / "data" / "runtime.sqlite3"
    if path.is_symlink():
        raise ValueError("数据库不能是符号链接")
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
            app_id = con.execute("PRAGMA application_id").fetchone()[0]
            if app_id != APPLICATION_ID:
                raise ValueError("现有数据库不是 Futures 数据库，拒绝修改")
    elif not initialize:
        raise ValueError("尚未安装期货运行数据，请先执行菜单 1")
    store = Store(root)
    return store.initialize() if initialize else store


def _username(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", value):
        raise ValueError("用户名须为 1–64 位英文、数字或 . _ @ -")
    return value


def _password(value, *, allow_blank=False):
    if value == "" and allow_blank:
        return value
    if not isinstance(value, str) or not 6 <= len(value) <= 256 or not value.isprintable():
        raise ValueError("密码须为 6–256 个可打印字符")
    return value


def _read_password(from_stdin=False):
    if from_stdin or not sys.stdin.isatty():
        value = sys.stdin.readline(4098)
        if len(value) > 4097:
            raise ValueError("密码输入过长")
        return value.rstrip("\r\n")
    return getpass.getpass("新密码（至少 6 位；首次初始化回车自动生成）: ")


def _web(values):
    values = values or {}
    raw_port = values.get("port", 8197)
    if isinstance(raw_port, bool) or not re.fullmatch(r"\d{1,5}", str(raw_port)):
        raise ValueError("网页端口须为 1–65535 的整数")
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise ValueError("网页端口须为 1–65535 的整数")
    host = str(values.get("host", "0.0.0.0")).strip()
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        raise ValueError("监听地址须为 IP，例如 0.0.0.0、127.0.0.1 或 ::") from None
    return {"host": host, "port": port}


def read_web_configuration(root=None):
    store = _store(root or BASE_DIR)
    with store.read() as con:
        return _web(store.setting(con, "web", {}))


def check_port_available(host, port):
    web = _web({"host": host, "port": port})
    family = socket.AF_INET6 if ":" in web["host"] else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            listener.bind((web["host"], web["port"]))
    except OSError as error:
        raise ValueError(f"地址 {web['host']}:{web['port']} 无法监听（端口占用或地址不可用）：{error.strerror}") from None


def _service_active(unit, timeout):
    try:
        result = subprocess.run(["systemctl", "is-active", "--quiet", unit],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _web_healthy(web, timeout):
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(web["host"], web["host"])
    if ":" in host:
        host = f"[{host}]"
    url = f"http://{host}:{web['port']}/health"
    try:
        # A local readiness check must not be routed through HTTP_PROXY.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            body = response.read(4097)
            if response.status != 200 or len(body) > 4096:
                return False
            result = json.loads(body)
            return (isinstance(result, dict) and result.get("application") == "futures"
                    and result.get("status") == "ok")
    except (OSError, ValueError, urllib.error.URLError):
        return False


def wait_ready(store, scope="all", timeout=10):
    if scope not in ("all", "web", "worker"):
        raise ValueError("就绪检查范围无效")
    if isinstance(timeout, bool) or not 0 < float(timeout) <= 30:
        raise ValueError("就绪等待须在 0–30 秒内")
    units = ["futures-web.service", "futures-worker.service"] if scope == "all" else [f"futures-{scope}.service"]
    with store.read() as con:
        web = backend_web(store.root, _web(store.setting(con, "web", {})))
    deadline = time.monotonic() + float(timeout)
    consecutive = 0
    detail = "等待服务启动"
    while time.monotonic() < deadline:
        inactive = []
        for unit in units:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not _service_active(unit, min(0.7, remaining)):
                inactive.append(unit)
        healthy = not inactive
        if inactive:
            detail = "服务尚未运行：" + "、".join(inactive)
        elif scope in ("all", "web"):
            remaining = deadline - time.monotonic()
            healthy = remaining > 0 and _web_healthy(web, min(1.2, remaining))
            if not healthy:
                detail = "网页 /health 尚未返回本项目的健康状态"
        consecutive = consecutive + 1 if healthy else 0
        # systemd Type=simple reports startup before Gunicorn has bound its
        # socket. Two healthy observations avoid claiming an instant restart
        # loop as a completed installation.
        if consecutive >= 2:
            return {"ready": True, "scope": scope, "detail": "服务状态与网页健康检查已通过" if scope != "worker" else "行情服务已运行"}
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.3, remaining))
    raise ValueError(f"{float(timeout):g} 秒内服务未就绪：{detail}；请查看对应服务日志")


def initialize_account(store, username="admin", password="", port=None, host=None):
    """Idempotent init: an existing account and port are never overwritten."""
    with store.read() as con:
        existing = store.setting(con, "auth", {})
        web = _web(store.setting(con, "web", {}))
    if existing:
        return {"created": False, "web": web,
                "detail": "已有账户和运行设置已保留"}
    password = _password(password or "".join(secrets.choice("23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz") for _ in range(6)))
    web = _web({"host": host if host is not None else web["host"], "port": port if port is not None else web["port"]})
    password_hash = generate_password_hash(password, method="pbkdf2:sha256:1000000")
    credentials = store.root / "initial_credentials.txt"
    written = False
    try:
        with store.tx() as con:
            existing = store.setting(con, "auth", {})
            if existing:
                return {"created": False,
                        "web": _web(store.setting(con, "web", {})), "detail": "已有账户和运行设置已保留"}
            text = (
                "闲云期货 · 首次安装凭据\n"
                f"创建时间：{stamp()}\n密码：{password}\n"
                f"网页地址：http://<服务器IP>:{web['port']}/\n"
                "本文件只保存首次安装值，修改账户后不会自动更新。\n"
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(credentials, flags, 0o600)
            except FileExistsError:
                raise ValueError("首次凭据文件已存在但数据库没有账户，请核对并移走旧 initial_credentials.txt 后重试") from None
            written = True
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            store.set_setting(con, "auth", {"password_hash": password_hash, "version": 1})
            store.set_setting(con, "web", web)
    except BaseException:
        if written:
            credentials.unlink(missing_ok=True)
        raise
    return {"created": True, "web": web, "credentials_file": str(credentials),
            "detail": "登录密码已创建；首次凭据文件权限为 600"}


def change_account(store, username=None, password=""):
    """Password-only login. Keep username parameter for older CLI callers."""
    password = _password(password, allow_blank=True)
    with store.tx() as con:
        auth = store.setting(con, "auth", {})
        if not auth:
            raise ValueError("尚未配置登录密码，请先初始化")
        if password:
            auth = {"password_hash": generate_password_hash(password, method="pbkdf2:sha256:1000000"),
                    "version": int(auth.get("version", 0)) + 1 if isinstance(auth.get("version", 0), int)
                    else secrets.token_hex(16)}
            store.set_setting(con, "auth", auth)
            con.execute("DELETE FROM login_limits")
    return {"changed": bool(password), "detail": "登录密码已修改，所有旧会话失效" if password else "登录密码保持不变"}


def change_port(store, port=None, host=None, check=False):
    with store.read() as con:
        current = _web(store.setting(con, "web", {}))
    desired = _web({"port": current["port"] if port is None else port,
                    "host": current["host"] if host is None else host})
    changed = desired != current
    if check and changed:
        check_port_available(desired["host"], desired["port"])
    if changed:
        with store.tx() as con:
            store.set_setting(con, "web", desired)
    return {**desired, "changed": changed, "detail": "端口已保存，重启网页服务后生效" if changed else "端口未改变"}


def set_backup_target(store, target):
    with store.read() as con:
        cfg = {**SYSTEM_DEFAULTS, **store.setting(con, "system", {})}
    cfg["backup_target"] = str(target or "").strip()
    validated = validate_system(cfg)
    effective = validated["backup_target"]
    if effective.startswith("/"):
        from backup import _remote_root
        backup_writable_path(store.root, effective, prepare=True)
    changed = validated != cfg
    with store.tx() as con:
        store.set_setting(con, "system", validated)
    if not effective:
        location = str(store.root / "backup")
    elif effective.startswith("/"):
        location = str(Path(effective) if Path(effective).name.lower() == "futures" else Path(effective) / "futures")
    else:
        from backup import _remote_root
        location = _remote_root(effective)
    return {"changed": changed, "backup_target": effective,
            "effective_path": location,
            "detail": "已设置默认备份路径；还会在项目 backup/ 保存本地恢复副本"}


def public_configuration(store):
    with store.read() as con:
        auth = store.setting(con, "auth", {})
        push = dict(store.setting(con, "push", {}))
        for key in SECRET_KEYS:
            if key in push:
                push[key] = "已配置" if push[key] else ""
        return {"web": _web(store.setting(con, "web", {})),
                "auth": {"configured": bool(auth), "mode": "password-only"},
                "system": {**SYSTEM_DEFAULTS, **store.setting(con, "system", {})},
                "push": push}


def status_data(store, integrity=False):
    configuration = public_configuration(store)
    with store.read() as con:
        heartbeat = store.meta(con, "heartbeat", {})
        backup = store.meta(con, "backup_status", {})
        counts = {name: con.execute(f"SELECT COUNT(*) FROM {name}" + (" WHERE active=1" if name == "symbols" else "")).fetchone()[0]
                  for name in ("symbols", "events", "snapshots")}
        counts["pending_push"] = con.execute("SELECT COUNT(*) FROM outbox WHERE status IN ('pending','sending')").fetchone()[0]
        database_check = con.execute("PRAGMA quick_check").fetchone()[0] if integrity else "未执行"
    age = None
    if heartbeat.get("time"):
        try:
            age = max(0, round((now_shanghai() - parse_timestamp(heartbeat["time"])).total_seconds()))
        except ValueError:
            pass
    return {"root": str(store.root), "database": str(store.path),
            "database_check": database_check, "web": configuration["web"], "auth": configuration["auth"],
            "strategy_enabled": bool(configuration["system"]["strategy_enabled"]),
            "refresh_seconds": configuration["system"]["refresh_seconds"], "quote_source": configuration["system"]["quote_source"],
            "heartbeat": heartbeat, "heartbeat_age_seconds": age, "backup_status": backup, "counts": counts}


def print_status(result):
    heartbeat = result["heartbeat"]
    print(f"项目目录：{result['root']}")
    print(f"外部访问端口：{result['web']['port']}")
    print(f"登录方式：{'仅密码' if result['auth']['configured'] else '尚未配置'}")
    print(f"策略总开关：{'开启' if result['strategy_enabled'] else '关闭'}；刷新间隔：{result['refresh_seconds']} 秒")
    print(f"活动标的：{result['counts']['symbols']}；策略/交易记录：{result['counts']['events']}；待推送：{result['counts']['pending_push']}")
    print(f"行情心跳：{heartbeat.get('time') or '暂无'}；状态：{heartbeat.get('status') or '尚未启动'}")
    if result["heartbeat_age_seconds"] is not None:
        print(f"心跳距今：{result['heartbeat_age_seconds']} 秒")
    if heartbeat.get("detail"):
        print("后台说明：" + str(heartbeat["detail"])[:500])
    backup = result["backup_status"]
    print(f"最近备份：{backup.get('time') or '暂无'}；{backup.get('detail') or '尚未备份'}")
    if result["database_check"] != "未执行":
        print("数据库检查：" + result["database_check"])


def _systemd_quote(value):
    text = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("服务配置路径不能包含换行或控制字符")
    # systemd.syntax(7): quote a whole item, double backslashes.  unit(5): %%
    # is a literal percent rather than a systemd specifier.
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def backup_writable_path(root, target, prepare=False):
    target = str(target or "")
    if not target or not target.startswith("/"):
        return None
    _systemd_quote(target)
    parent = Path(target)
    if ".." in parent.parts or len(parent.parts) < 2 or parent.parts[1] in PROTECTED_TOP:
        raise ValueError("外部备份路径不能位于受保护系统目录")
    if any(part.lower() in PROTECTED_COMPONENTS for part in parent.parts):
        raise ValueError("外部备份路径不能位于 quant/nginx 目录")
    if parent in {Path("/"), Path("/root"), Path("/home"), Path("/var"), Path("/opt"), Path("/tmp")}:
        raise ValueError("请使用独立的外部备份父目录")
    _no_symlink_components(parent)
    if not parent.is_dir():
        if (prepare and parent.name.lower() == "futures" and parent.parent.is_dir()
                and not parent.is_symlink()):
            parent.mkdir(mode=0o700, exist_ok=True)
        else:
            raise ValueError("外部备份目录不存在或挂载未就绪。请先挂载目录，或使用 /mnt/rclone/futures")
    destination = parent if parent.name.lower() == "futures" else parent / "futures"
    if destination.is_symlink():
        raise ValueError("外部 futures 备份目录不能是符号链接")
    if destination == root or root in destination.parents or destination in root.parents:
        raise ValueError("外部备份不能嵌套在程序目录内或覆盖程序目录")
    if destination.exists() and not destination.is_dir():
        raise ValueError("外部 futures 备份路径已被普通文件占用")
    if prepare:
        destination.mkdir(mode=0o700, exist_ok=True)
    return destination


def rclone_configuration():
    """Find the actual config without dumping remotes or authentication tokens.

    rclone writes OAuth refresh tokens using temporary files and rename in the
    configuration's real parent directory. A file-only allow-list is therefore
    insufficient. The original shared config remains authoritative; we never
    create a second OAuth-token copy that could diverge from quant's config.
    """
    executable = shutil.which("rclone")
    if not executable:
        return None
    try:
        process = subprocess.run([executable, "config", "file"], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"warning": "无法检测 rclone 配置路径；配置完成后请重启行情服务。"}
    if process.returncode:
        return {"warning": "rclone 未能返回配置路径；配置完成后请重启行情服务。"}
    candidates = [line.strip() for line in process.stdout.splitlines() if line.strip().startswith("/")]
    if not candidates:
        return {"warning": "尚未找到 rclone 配置；配置完成后请重启行情服务。"}
    config = Path(candidates[-1]).resolve()
    _systemd_quote(config)
    if not config.is_file():
        return {"warning": "rclone 配置文件尚不存在；配置完成后请重启行情服务。"}
    parent = config.parent
    # Allow conventional dedicated config directories, including
    # /root/.config/rclone, /etc/rclone and /volume1/docker/rclone/config.
    dedicated = any(re.fullmatch(r"rclone(?:[-_][A-Za-z0-9_.-]+)?", part.lower())
                    for part in parent.parts[-2:])
    blocked = parent in {Path("/"), Path("/root"), Path("/home"), Path("/etc"), Path("/var"), Path("/tmp"), Path("/opt")}
    blocked = blocked or any(part.lower() in PROTECTED_COMPONENTS for part in parent.parts)
    if blocked or not dedicated:
        return {"config": config, "warning":
                f"rclone 配置位于 {parent}，未放开该目录写入；OAuth 刷新可能失败。请迁移到专用 rclone 配置目录后重启行情服务。"}
    return {"config": config, "directory": parent}


def render_unit(store, kind, prepare=False):
    if kind not in ("web", "worker"):
        raise ValueError("服务类型须为 web 或 worker")
    root = validate_project_root(store.root)
    writable = [root / "data", root / "backup"]
    extra_environment = []
    if prepare:
        for path in writable:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if kind == "worker":
        with store.read() as con:
            cfg = {**SYSTEM_DEFAULTS, **store.setting(con, "system", {})}
        extra = backup_writable_path(root, cfg.get("backup_target"), prepare)
        if extra:
            writable.append(extra)
        rclone = rclone_configuration()
        if rclone:
            if rclone.get("config"):
                extra_environment.append("Environment=" + _systemd_quote("RCLONE_CONFIG=" + str(rclone["config"])))
            if rclone.get("directory") and rclone["directory"] not in writable:
                writable.append(rclone["directory"])
            if rclone.get("warning") and prepare:
                print("提示：" + rclone["warning"], file=sys.stderr)
    python = root / ".venv" / "bin" / "python"
    if kind == "web":
        from nginx_proxy import proxy_state
        current_proxy = proxy_state(root)
        if current_proxy.get("enabled"):
            extra_environment.append("Environment=" + _systemd_quote("FUTURES_COOKIE_SECURE=" +
                                      ("1" if current_proxy.get("https") else "0")))
        command = " ".join((_systemd_quote(python), "-m gunicorn --config", _systemd_quote(root / "gunicorn_conf.py"), '"app:create_app()"'))
        description = "闲云期货网页服务"
    else:
        command = " ".join((_systemd_quote(python), _systemd_quote(root / "runtime.py"), "--root", _systemd_quote(root)))
        description = "闲云期货行情与推送服务"
    return "\n".join([
        f"# futures.sh project-root: {root}",
        "[Unit]", f"Description={description}", "Wants=network-online.target", "After=network-online.target",
        "StartLimitIntervalSec=60", "StartLimitBurst=5", "", "[Service]", "Type=simple", "User=root", "Group=root",
        # The project root is deliberately restricted to an ASCII path without
        # whitespace. WorkingDirectory is a single path, not a quoted word list.
        "WorkingDirectory=" + str(root),
        "Environment=" + _systemd_quote("FUTURES_HOME=" + str(root)),
        "Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1",
        "Environment=" + _systemd_quote("XDG_CACHE_HOME=" + str(root / "data" / "cache")),
        "Environment=" + _systemd_quote("RCLONE_CACHE_DIR=" + str(root / "data" / "rclone-cache")),
        *extra_environment,
        "ExecStart=" + command,
        "Restart=on-failure", "RestartSec=10", "TimeoutStopSec=60", "UMask=0077",
        "NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=read-only", "PrivateTmp=true",
        "ProtectKernelTunables=true", "ProtectKernelModules=true", "ProtectControlGroups=true", "RestrictSUIDSGID=true",
        "ReadWritePaths=" + " ".join(_systemd_quote(path) for path in writable),
        "StandardOutput=journal", "StandardError=journal", f"SyslogIdentifier=futures-{kind}",
        "", "[Install]", "WantedBy=multi-user.target", "",
    ])


def parse_mountpoints(content):
    """Decode Linux mountinfo's octal-escaped mount-point field, including bind mounts."""
    points = set()
    for line in content.splitlines():
        fields = line.split()
        if len(fields) < 7 or "-" not in fields:
            continue
        mountpoint = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4])
        if mountpoint.startswith("/"):
            points.add(Path(mountpoint))
    return points


def mounted_paths():
    try:
        return parse_mountpoints(Path("/proc/self/mountinfo").read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError("无法检查系统挂载信息，拒绝删除项目目录") from error


def validate_deletion_root(root):
    root = validate_project_root(root)
    marker = root / OWNER_MARKER
    if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != f"futures|{root}":
        raise ValueError("目录缺少匹配的期货项目归属标记，拒绝删除")
    if os.path.ismount(root):
        raise ValueError("项目目录是挂载点，拒绝整体删除")
    # os.path.ismount alone misses bind mounts on the same filesystem.
    for mountpoint in mounted_paths():
        if mountpoint == root or root in mountpoint.parents:
            raise ValueError(f"项目目录或子目录存在挂载点 {mountpoint}，请先卸载挂载后再删除")
    for directory, names, _ in os.walk(root, followlinks=False):
        for name in names:
            child = Path(directory) / name
            if child.is_symlink():
                continue
            if os.path.ismount(child):
                raise ValueError(f"目录内存在挂载点 {child}，请先卸载挂载后再删除")
    return root


def _json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(BASE_DIR), help="期货项目绝对路径")
    commands = parser.add_subparsers(dest="command", required=True, title="管理命令")
    init = commands.add_parser("init", help="初始化运行数据及首次登录密码，已有密码不覆盖")
    init.add_argument("--username", default="admin", help=argparse.SUPPRESS)
    init.add_argument("--password-stdin", action="store_true", help="从标准输入读取密码；空行生成随机密码")
    init.add_argument("--port", type=int)
    init.add_argument("--host")
    init.add_argument("--check-port", action="store_true", help="首次创建账户前检查端口可用性")
    account = commands.add_parser("account", help="修改登录密码；空密码保持原密码")
    account.add_argument("--username", help=argparse.SUPPRESS)
    account.add_argument("--password-stdin", action="store_true")
    port = commands.add_parser("port", help="读取或修改网页监听端口")
    port.add_argument("--port", type=int)
    port.add_argument("--host")
    port.add_argument("--check", action="store_true", help="保存新端口前检查能否监听")
    for name in ("status", "diagnose"):
        status = commands.add_parser(name, help="显示运行数据状态" if name == "status" else "检查数据库和运行状态")
        status.add_argument("--json", action="store_true")
    config = commands.add_parser("config", help="读取脱敏配置")
    config.add_argument("--field", help="读取公开字段，例如 web.port、auth.configured")
    quote = commands.add_parser("quote", help="独立检测三个行情采集通道，不运行策略也不推送")
    quote.add_argument("symbol", help="具体交割月合约，例如 m2709")
    quote.add_argument("--exchange")
    quote.add_argument("--timeout", type=float, default=8)
    quote.add_argument("--json", action="store_true")
    unit = commands.add_parser("unit", help="生成服务配置，供管理脚本使用")
    unit.add_argument("--kind", choices=("web", "worker"), required=True)
    unit.add_argument("--prepare-paths", action="store_true")
    backup_path = commands.add_parser("backup-target", help="设置默认备份路径（/mnt/rclone/futures 或 remote:futures）")
    backup_path.add_argument("--path", required=True)
    ready = commands.add_parser("ready", help="有界等待服务和网页健康检查就绪")
    ready.add_argument("--scope", choices=("all", "web", "worker"), default="all")
    ready.add_argument("--timeout", type=float, default=10)
    commands.add_parser("can-delete", help="检查专用目录是否可由管理脚本删除")
    args = parser.parse_args(argv)
    try:
        if args.command == "can-delete":
            _json({"root": str(validate_deletion_root(args.root)), "safe": True})
            return 0
        if args.command == "quote":
            contract = normalize_contract(args.symbol, args.exchange, as_of=now_shanghai())
            quotes = collect_quotes(contract, args.timeout)
            for quote in quotes:
                quote["fresh"] = is_fresh(quote)
            if args.json:
                _json({"contract": contract, "quotes": quotes, "detail": "独立行情检测，未修改任何策略、交易或推送记录"})
            else:
                print(f"合约：{contract['symbol']} · {contract['name']} · {contract['exchange_name']}")
                for quote in quotes:
                    print(f"\n{quote['label']}：{'返回有效报价' if quote['ok'] else '失败'}")
                    print(f"  最新价：{quote.get('price') if quote.get('price') is not None else '—'}；行情时间：{quote.get('quote_at') or '—'}")
                    print(f"  新鲜度：{'有效' if quote['fresh'] else '超时/休市报价或无效'}；耗时：{quote.get('latency_ms', 0)} ms")
                    if quote.get("error"):
                        print("  说明：" + quote["error"])
                print("\n本次只读取公开行情，不推进策略、不记录交易、不发送推送。")
            return 0 if any(quote.get("ok") for quote in quotes) else 1
        store = _store(args.root, initialize=args.command == "init")
        if args.command == "init":
            with store.read() as con:
                existing = store.setting(con, "auth", {})
                current_web = _web(store.setting(con, "web", {}))
            if args.check_port and not existing:
                candidate = _web({"host": args.host if args.host is not None else current_web["host"],
                                  "port": args.port if args.port is not None else current_web["port"]})
                check_port_available(candidate["host"], candidate["port"])
            password = "" if existing else _read_password(args.password_stdin)
            result = initialize_account(store, args.username, password, args.port, args.host)
            _json(result)
        elif args.command == "account":
            _json(change_account(store, args.username, _read_password(args.password_stdin)))
        elif args.command == "port":
            _json(change_port(store, args.port, args.host, args.check))
        elif args.command in ("status", "diagnose"):
            result = status_data(store, args.command == "diagnose")
            _json(result) if args.json else print_status(result)
        elif args.command == "config":
            result = public_configuration(store)
            if args.field:
                value = result
                for key in args.field.split("."):
                    if key in SECRET_KEYS or not isinstance(value, dict) or key not in value:
                        raise ValueError("该配置字段不存在或不允许通过命令行显示")
                    value = value[key]
                if isinstance(value, (dict, list)):
                    _json(value)
                elif isinstance(value, bool):
                    print("true" if value else "false")
                else:
                    print(value)
            else:
                _json(result)
        elif args.command == "backup-target":
            _json(set_backup_target(store, args.path))
        elif args.command == "unit":
            print(render_unit(store, args.kind, args.prepare_paths), end="")
        elif args.command == "ready":
            _json(wait_ready(store, args.scope, args.timeout))
    except (ValueError, OSError, sqlite3.Error) as error:
        print("错误：" + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
