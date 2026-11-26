"""Futures configuration and Shanghai-time scheduling (quant-compatible)."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from market_data import SOURCE_ORDER

APP_NAME = "闲云期货"
SCHEMA_VERSION = 1
APPLICATION_ID = 1179997266  # FUTR
SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE_DIR = Path(os.environ.get("FUTURES_HOME", Path(__file__).resolve().parent)).resolve()

SYSTEM_DEFAULTS = {
    "strategy_enabled": True,
    "quote_source": "auto", "refresh_seconds": 60, "quote_max_age": 180,
    "source_spread_ticks": 10,
    "day_start": "09:00", "day_end": "15:00",
    "breaks": "10:15-10:30,11:30-13:30",
    "night_enabled": True, "night_start": "21:00", "night_end": "23:00",
    "daily_snapshot_time": "08:00", "backup_time": "08:00",
    "timezone": "Asia/Shanghai", "closed_dates": "",
    "backup_enabled": False, "backup_target": "", "local_keep": 1,
    "backup_keep_days": 30, "history_keep_days": 90,
}
PUSH_DEFAULTS = {
    "PUSH_ENABLED": "no", "PUSH_CHANNEL": "ntfy",
    "NTFY_URL": "http://127.0.0.1:8083", "NTFY_TOPIC": "futures",
    "NTFY_USERNAME": "", "NTFY_PASSWORD": "", "NTFY_PRIORITY": "4",
    "GOTIFY_URL": "", "GOTIFY_TOKEN": "", "GOTIFY_PRIORITY": "10",
}


def now_shanghai():
    return datetime.now(SHANGHAI)


def stamp(value=None):
    return (value or now_shanghai()).astimezone(SHANGHAI).isoformat(timespec="seconds")


def parse_time(value):
    if not re.fullmatch(r"\d{2}:\d{2}", str(value)):
        raise ValueError("时间须为 HH:MM，例如 09:00")
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("时间超出有效范围") from exc


def parse_timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=SHANGHAI)
        return result.astimezone(SHANGHAI)
    except (TypeError, ValueError) as exc:
        raise ValueError("无法识别行情时间") from exc


def as_bool(value):
    return value is True or str(value).lower() in {"1", "yes", "true", "on"}


def integer(value, label, low, high):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value).strip()):
        raise ValueError(f"{label}必须是整数")
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f"{label}范围为 {low}–{high}")
    return number


def validate_system(values):
    cfg = dict(SYSTEM_DEFAULTS)
    cfg.update({k: v for k, v in values.items() if k in cfg})
    for key in ("strategy_enabled", "night_enabled", "backup_enabled"):
        cfg[key] = as_bool(cfg[key])
    for key, label, lo, hi in (
        ("refresh_seconds", "刷新间隔", 15, 3600),
        ("quote_max_age", "行情有效秒数", 30, 1800),
        ("source_spread_ticks", "数据源价差上限", 1, 10000),
        ("local_keep", "本地备份份数", 1, 30),
        ("backup_keep_days", "远端备份保留天数", 1, 365),
        ("history_keep_days", "行情/常规快照保留天数", 7, 730),
    ):
        cfg[key] = integer(cfg[key], label, lo, hi)
    for key in ("day_start", "day_end", "night_start", "night_end", "daily_snapshot_time", "backup_time"):
        cfg[key] = str(cfg[key]).strip()
        parse_time(cfg[key])
    if cfg["day_start"] >= cfg["day_end"]:
        raise ValueError("日盘开始时间必须早于结束时间；跨午夜请使用夜盘设置")
    if cfg["night_enabled"] and cfg["night_start"] == cfg["night_end"]:
        raise ValueError("夜盘开始与结束时间不能相同")
    cleaned = []
    for item in str(cfg["breaks"]).replace("，", ",").split(","):
        if not item.strip():
            continue
        pair = item.strip().split("-")
        if len(pair) != 2:
            raise ValueError("休息时段格式为 10:15-10:30,11:30-13:30")
        start, end = (x.strip() for x in pair)
        parse_time(start); parse_time(end)
        if start >= end:
            raise ValueError("休息时段开始必须早于结束")
        cleaned.append(f"{start}-{end}")
    cfg["breaks"] = ",".join(cleaned)
    if cfg["quote_source"] not in ("auto", *SOURCE_ORDER):
        raise ValueError("请选择有效的行情源")
    try:
        ZoneInfo(str(cfg["timezone"]))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("无效时区，请输入 IANA 名称，例如 Asia/Shanghai") from exc
    dates = []
    for item in re.split(r"[,，\s]+", str(cfg["closed_dates"]).strip()):
        if not item:
            continue
        try:
            parsed = datetime.strptime(item, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("休市日期须为 YYYY-MM-DD，可逐行输入") from exc
        dates.append(parsed.isoformat())
    cfg["closed_dates"] = "\n".join(sorted(set(dates)))
    target = str(cfg["backup_target"]).strip().rstrip("/")
    if target and not (target.startswith("/") or re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9_./-]*", target)):
        raise ValueError("备份父路径须为绝对路径或 rclone:remote/path 格式")
    if target in {"/", "/root", "/etc", "/usr", "/var"} or ".." in target.split("/"):
        raise ValueError("请使用专用备份父目录")
    cfg["backup_target"] = target
    return cfg


def session_info(cfg, at=None, symbol_config=None, contract=None):
    """Natural-day windows; Friday night continues Saturday, never Sunday night.

    Closed dates disable that natural day's sessions plus the preceding evening
    (Friday is the preceding evening for a Monday closure). No guessed holiday API.
    """
    at = (at or now_shanghai()).astimezone(SHANGHAI)
    day, current = at.date(), at.time().replace(tzinfo=None)
    cfg = {**SYSTEM_DEFAULTS, **cfg}
    closed = set(str(cfg.get("closed_dates", "")).split())
    symbol_config = symbol_config or {}
    contract = contract or {}
    def business(d):
        return d.weekday() < 5 and d.isoformat() not in closed
    start, end = parse_time(cfg["day_start"]), parse_time(cfg["day_end"])
    if business(day) and start <= current <= end:
        paused = any(parse_time(a) <= current < parse_time(b)
                     for a, b in (part.split("-") for part in cfg["breaks"].split(",") if part))
        if not paused:
            return {"open": True, "label": "日盘", "session_day": str(day)}
    if cfg["night_enabled"] and symbol_config.get("night_end") != "off" and contract.get("exchange") != "CFFEX":
        ns = parse_time(cfg["night_start"])
        ne = parse_time(symbol_config.get("night_end") or cfg["night_end"])
        base = day
        in_night = ns <= current <= ne if ns < ne else (current >= ns or current <= ne)
        if ns > ne and current <= ne:
            base -= timedelta(days=1)
        next_day = base + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        if in_night and business(base) and base.isoformat() not in closed and next_day.isoformat() not in closed:
            return {"open": True, "label": "夜盘", "session_day": str(base)}
    return {"open": False, "label": "休市/非监测时段", "session_day": str(day)}


def default_symbol_config(contract):
    return {
        "enabled": True, "lower_price": "", "upper_price": "",
        "down_step": "100", "up_step": "100", "signal_lots": 1,
        "multiplier": str(contract.get("multiplier") or 1),
        "tick_size": str(contract.get("tick_size") or 1),
        "monitor_step": "100",
        "night_end": "", "source": "inherit", "rearm_mode": "return",
    }


def backup_display_state(config, attempt=None, last_success=None, at=None, grace_minutes=30):
    """Evaluate the latest scheduled backup independently of the last attempt.

    Only a completed, successful backup updates the displayed timestamp. A
    failed attempt or an overdue rotation turns that *same* timestamp red.
    All pages call this on each GET, including after the worker stops.
    Schedules use Shanghai time regardless of the server display timezone.
    """
    now = (at or now_shanghai()).astimezone(SHANGHAI)
    cfg = {**SYSTEM_DEFAULTS, **(config or {})}
    attempt = attempt if isinstance(attempt, dict) else {}
    success = last_success if isinstance(last_success, dict) else {}
    state = attempt.get("status", "none")
    # Legacy v1.4.7 stored the completed time only in backup_status.
    if not success.get("time") and state == "ok":
        success = {"time": attempt.get("time"), "path": attempt.get("path", "")}
    successful_at = None
    if success.get("time"):
        try:
            successful_at = parse_timestamp(success["time"])
            if successful_at > now + timedelta(minutes=5):
                successful_at = None  # Bad clock data is never a green success.
        except ValueError:
            pass
    successful_time = stamp(successful_at) if successful_at else None
    last_text = successful_at.strftime("%Y-%m-%d %H:%M:%S") if successful_at else "尚无成功备份"
    details = str(attempt.get("detail") or "").strip()
    state_color, message = "", "尚未备份；可在系统页执行立即备份"
    deadline = None
    overdue = False
    if as_bool(cfg.get("backup_enabled")):
        scheduled = datetime.combine(now.date(), parse_time(cfg.get("backup_time", "08:00")), tzinfo=SHANGHAI)
        # Allow slow I/O and queued work a limited grace period; afterwards,
        # yesterday's green badge cannot mask today's missing backup.
        required = scheduled if now >= scheduled + timedelta(minutes=grace_minutes) else scheduled - timedelta(days=1)
        deadline = required.strftime("%Y-%m-%d %H:%M")
        overdue = successful_at is None or successful_at < required
    running_stuck = False
    if state == "running" and attempt.get("time"):
        try:
            running_stuck = (now - parse_timestamp(attempt["time"])).total_seconds() > 3600
        except ValueError:
            running_stuck = True
    if state == "error":
        state_color = "bad"
        message = "最近一次备份失败" + (f"：{details}" if details else "")
    elif running_stuck:
        state_color = "bad"
        message = "备份任务已运行超过一小时或状态未更新，请检查后台服务"
    elif state == "restored":
        state_color = "bad"
        message = "运行数据已重建，请生成一次新的成功备份"
    elif overdue:
        state_color = "bad"
        message = f"轮转备份未按计划更新（应在 {deadline} 上海时间之后成功）"
        if state == "running":
            message += "；当前任务仍在执行"
    elif state == "running":
        state_color = "warn"
        message = "正在执行备份，最近成功时间保持不变"
    elif successful_at is not None:
        state_color = "good"
        message = "备份成功，按计划更新" if as_bool(cfg.get("backup_enabled")) else "最近一次备份成功（自动轮转未启用）"
    elif not as_bool(cfg.get("backup_enabled")):
        message = "自动轮转未启用，尚无成功备份"
    tip = f"最近成功（上海时间）：{last_text}；状态：{message}"
    return {"color": state_color, "last_success_time": successful_time,
            "last_success_text": last_text, "message": message, "tooltip": tip,
            "latest_status": state, "overdue": overdue}
