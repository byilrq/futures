"""Quant-compatible ntfy/Gotify delivery with a transactional outbox."""
from __future__ import annotations

import base64
import json
import re
import socket
from email.header import Header
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from config import PUSH_DEFAULTS, integer, stamp


def validate_config(values, old=None):
    cfg = {**PUSH_DEFAULTS, **(old or {})}
    for key in PUSH_DEFAULTS:
        if key in values:
            # Passwords are literal. An empty field means preserve the saved value.
            value = str(values[key])
            if key in {"NTFY_PASSWORD", "GOTIFY_TOKEN"}:
                if value:
                    cfg[key] = value
            else:
                cfg[key] = value.strip()
    cfg["PUSH_ENABLED"] = "yes" if str(cfg["PUSH_ENABLED"]).lower() in {"yes", "1", "on", "true"} else "no"
    if cfg["PUSH_CHANNEL"] not in {"ntfy", "gotify"}:
        raise ValueError("推送通道必须是 ntfy 或 Gotify")
    for key in ("NTFY_URL", "GOTIFY_URL"):
        if not cfg[key]:
            continue
        parsed = urlsplit(cfg[key])
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or any(x in cfg[key] for x in "\r\n")):
            raise ValueError("推送地址须为 HTTP/HTTPS 服务地址，认证请填专用字段")
        cfg[key] = cfg[key].rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", cfg["NTFY_TOPIC"]):
        raise ValueError("ntfy Topic 使用英文字母、数字、短横线或下划线")
    cfg["NTFY_PRIORITY"] = str(integer(cfg["NTFY_PRIORITY"], "ntfy 优先级", 1, 5))
    cfg["GOTIFY_PRIORITY"] = str(integer(cfg["GOTIFY_PRIORITY"], "Gotify 优先级", 0, 10))
    for key in ("NTFY_USERNAME", "NTFY_PASSWORD", "GOTIFY_TOKEN"):
        if len(cfg[key]) > 4096 or "\r" in cfg[key] or "\n" in cfg[key]:
            raise ValueError("认证字段长度或格式不正确")
    if str(values.get("clear_gotify_token", "")).lower() in {"1", "on", "yes"}:
        cfg["GOTIFY_TOKEN"] = ""
    return cfg


def send_message(cfg, title, body, opener=urlopen):
    """Return (sent/failed/unknown, detail). Never expose credential URLs.

    A timeout is ambiguous: the receiver may already have accepted the message.
    It remains visible for explicit retry instead of automatic duplicate delivery.
    """
    headers = {"User-Agent": "Futures-Monitor/1.0"}
    if cfg["PUSH_CHANNEL"] == "ntfy":
        if not cfg.get("NTFY_URL") or not cfg.get("NTFY_TOPIC"):
            return "failed", "缺少 ntfy 服务地址或 Topic"
        url = cfg["NTFY_URL"] + "/" + quote(cfg["NTFY_TOPIC"], safe="")
        headers.update({"Priority": str(cfg["NTFY_PRIORITY"]), "Markdown": "yes",
                        "Content-Type": "text/plain; charset=utf-8"})
        if title:
            headers["Title"] = Header(str(title), "utf-8").encode().replace("\n", "")
        if cfg.get("NTFY_USERNAME"):
            credentials = (cfg["NTFY_USERNAME"] + ":" + cfg.get("NTFY_PASSWORD", "")).encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
        payload = body.encode("utf-8")
    else:
        if not cfg.get("GOTIFY_URL") or not cfg.get("GOTIFY_TOKEN"):
            return "failed", "缺少 Gotify 服务地址或应用 Token"
        url = cfg["GOTIFY_URL"] + "/message?" + urlencode({"token": cfg["GOTIFY_TOKEN"]})
        headers["Content-Type"] = "application/json; charset=utf-8"
        gotify_message = {"message": body, "priority": int(cfg["GOTIFY_PRIORITY"])}
        if title:  # A daily snapshot intentionally has no separate transport title.
            gotify_message["title"] = title
        payload = json.dumps(gotify_message, ensure_ascii=False).encode("utf-8")
    try:
        req = Request(url, data=payload, headers=headers, method="POST")
        with opener(req, timeout=12) as response:
            code = response.status
            response.read(4096)
            if 200 <= code < 300:
                return "sent", f"{cfg['PUSH_CHANNEL']} 已接受消息，HTTP {code}"
            return "failed", f"推送服务返回 HTTP {code}"
    except HTTPError as exc:
        return "failed", f"推送服务返回 HTTP {exc.code}；请核对地址、认证或服务日志"
    except (URLError, TimeoutError, socket.timeout, OSError):
        return "unknown", "连接中断或超时，送达结果不确定；确认客户端后可手动重试"
    except (ValueError, UnicodeError):
        return "failed", "推送地址或编码无效，请检查配置"


def recover_inflight(store):
    with store.tx() as con:
        con.execute("UPDATE outbox SET status='unknown',detail='上次进程中断，送达结果不确定；未自动重发' WHERE status='sending'")
        store.prune_outbox(con)


def drain_outbox(store, limit=8, sender=send_message):
    delivered = 0
    for _ in range(limit):
        with store.tx() as con:
            row = con.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                break
            item = dict(row)
            cfg = {**PUSH_DEFAULTS, **store.setting(con, "push", {})}
            if cfg["PUSH_ENABLED"] != "yes" and not item["force"]:
                con.execute("UPDATE outbox SET status='disabled',detail='触发时推送总开关关闭，记录保留，不在之后补发' WHERE id=?", (item["id"],))
                store.prune_outbox(con)
                continue
            if item["symbol_id"] is not None:
                ev = con.execute("SELECT reverted_by FROM events WHERE id=?", (item["event_id"],)).fetchone()
                if ev and ev["reverted_by"]:
                    con.execute("UPDATE outbox SET status='cancelled',detail='该记录已撤销' WHERE id=?", (item["id"],))
                    store.prune_outbox(con)
                    continue
            con.execute("UPDATE outbox SET status='sending',attempts=attempts+1,claimed_at=? WHERE id=?", (stamp(), item["id"]))
        status, detail = sender(cfg, item["title"], item["body"])
        with store.tx() as con:
            con.execute("UPDATE outbox SET status=?,detail=?,delivered_at=? WHERE id=?", (status, detail, stamp(), item["id"]))
            store.prune_outbox(con)
        delivered += status == "sent"
    return delivered
