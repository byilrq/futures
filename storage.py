"""SQLite authoritative state, atomic snapshots, audit journal and push outbox."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager, closing
from pathlib import Path

from push_format import quant_style_message
from config import (APPLICATION_ID, BASE_DIR, SCHEMA_VERSION, SYSTEM_DEFAULTS,
                    PUSH_DEFAULTS, stamp)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)


def loads(value, default=None):
    return json.loads(value) if value is not None else default


class Conflict(ValueError):
    pass


# Recent push messages shown in the UI and kept in the SQLite outbox.
PUSH_LOG_LIMIT = 30


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS symbols(
 id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT NOT NULL,name TEXT NOT NULL,exchange TEXT NOT NULL,
 contract_json TEXT NOT NULL,config_json TEXT NOT NULL,state_json TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,active INTEGER NOT NULL DEFAULT 1,
 last_quote_epoch REAL NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS active_symbols ON symbols(symbol) WHERE active=1;
CREATE TABLE IF NOT EXISTS source_quotes(symbol_id INTEGER NOT NULL,source TEXT NOT NULL,payload TEXT NOT NULL,
 updated_at TEXT NOT NULL,PRIMARY KEY(symbol_id,source),FOREIGN KEY(symbol_id) REFERENCES symbols(id));
CREATE TABLE IF NOT EXISTS price_history(id INTEGER PRIMARY KEY,symbol_id INTEGER NOT NULL,
 time TEXT NOT NULL,epoch REAL NOT NULL,price TEXT NOT NULL,source TEXT NOT NULL,
 UNIQUE(symbol_id,epoch),FOREIGN KEY(symbol_id) REFERENCES symbols(id));
CREATE INDEX IF NOT EXISTS price_history_lookup ON price_history(symbol_id,epoch);
CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol_id INTEGER NOT NULL,
 time TEXT NOT NULL,kind TEXT NOT NULL,reason TEXT NOT NULL,price TEXT,
 config_json TEXT NOT NULL,state_json TEXT NOT NULL,event_id TEXT,ledger_seq INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(symbol_id) REFERENCES symbols(id));
CREATE INDEX IF NOT EXISTS snapshot_lookup ON snapshots(symbol_id,id DESC);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
 symbol_id INTEGER,time TEXT NOT NULL,kind TEXT NOT NULL,title TEXT NOT NULL,detail TEXT NOT NULL,
 side TEXT,lots INTEGER,price TEXT,snapshot_id INTEGER,reverted_by TEXT,
 FOREIGN KEY(symbol_id) REFERENCES symbols(id));
CREATE INDEX IF NOT EXISTS event_lookup ON events(symbol_id,seq DESC);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL,
 part INTEGER NOT NULL DEFAULT 1,symbol_id INTEGER,time TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,
 kind TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',detail TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL DEFAULT 0,claimed_at TEXT,delivered_at TEXT,hidden INTEGER NOT NULL DEFAULT 0,
 force INTEGER NOT NULL DEFAULT 0,UNIQUE(event_id,part));
CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,kind TEXT NOT NULL,symbol_id INTEGER,
 result TEXT NOT NULL,time TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',time TEXT NOT NULL,result TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS backtests(id TEXT PRIMARY KEY,symbol_id INTEGER,time TEXT NOT NULL,
 symbol TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_limits(key TEXT PRIMARY KEY,failures INTEGER NOT NULL,until REAL NOT NULL);
"""


class Store:
    def __init__(self, root=None):
        self.root = Path(root or BASE_DIR).resolve()
        self.data = self.root / "data"
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.data / "runtime.sqlite3"

    def connect(self):
        con = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def initialize(self):
        with closing(self.connect()) as con:
            app_id = con.execute("PRAGMA application_id").fetchone()[0]
            if app_id not in (0, APPLICATION_ID):
                raise ValueError("数据文件不是 Futures 数据库")
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(f"PRAGMA application_id={APPLICATION_ID}")
            con.executescript(SCHEMA)
            version = con.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if version and int(version[0]) != SCHEMA_VERSION:
                raise ValueError("数据库版本不兼容，请保留数据并升级程序")
            con.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            con.execute("INSERT OR IGNORE INTO metadata VALUES('secret_key',?)", (dumps(secrets.token_hex(32)),))
            con.execute("INSERT OR IGNORE INTO metadata VALUES('instance_id',?)", (dumps(secrets.token_hex(16)),))
            for key, value in (("system", SYSTEM_DEFAULTS), ("push", PUSH_DEFAULTS),
                               ("web", {"port": 8197, "host": "0.0.0.0"})):
                con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, dumps(value)))
            # V1.1 created strategy_enabled=false even if the user never touched it.
            # Preserve explicit shutdowns; upgrade an untouched default to ON once.
            if con.execute("SELECT 1 FROM metadata WHERE key='master_switch_v12_migrated'").fetchone() is None:
                old_system = self.setting(con, "system", SYSTEM_DEFAULTS)
                manually_closed = con.execute(
                    "SELECT 1 FROM events WHERE kind='settings' AND title='策略总开关' "
                    "AND detail LIKE '%关闭%' LIMIT 1").fetchone()
                if not old_system.get("strategy_enabled") and manually_closed is None:
                    # Opening on upgrade is just an administrative state flip:
                    # establish a new quote baseline for every symbol first.
                    old_system["strategy_enabled"] = True
                    for row in con.execute("SELECT id,state_json FROM symbols WHERE active=1").fetchall():
                        state = loads(row["state_json"], {})
                        state.setdefault("strategy", {})["needs_baseline"] = True
                        state.setdefault("monitor", {})["anchor"] = None
                        con.execute("UPDATE symbols SET state_json=? WHERE id=?", (dumps(state), row["id"]))
                    self.set_setting(con, "system", old_system)
                self.set_meta(con, "master_switch_v12_migrated", True)
            # Trim legacy logs on startup/upgrade without dropping queued messages.
            # A pending/sending outbox row is work, not disposable log history.
            self.prune_outbox(con)
        os.chmod(self.path, 0o600)
        return self

    @contextmanager
    def tx(self):
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @contextmanager
    def read(self):
        con = self.connect()
        try:
            con.execute("BEGIN")
            yield con
        finally:
            con.rollback()
            con.close()

    @staticmethod
    def setting(con, key, default=None):
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return loads(row[0], default) if row else default

    @staticmethod
    def set_setting(con, key, value):
        con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,revision=revision+1", (key, dumps(value)))

    @staticmethod
    def meta(con, key, default=None):
        row = con.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return loads(row[0], default) if row else default

    @staticmethod
    def set_meta(con, key, value):
        con.execute("INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))

    @staticmethod
    def decode_symbol(row):
        if row is None:
            return None
        result = dict(row)
        for col in ("contract", "config", "state"):
            result[col] = loads(result.pop(col + "_json"))
        return result

    def symbol(self, con, symbol_id, active=True):
        row = con.execute("SELECT * FROM symbols WHERE id=?" + (" AND active=1" if active else ""), (symbol_id,)).fetchone()
        if row is None:
            raise ValueError("标的不存在或已归档")
        return self.decode_symbol(row)

    def symbols(self, con):
        return [self.decode_symbol(r) for r in con.execute("SELECT * FROM symbols WHERE active=1 ORDER BY id")]

    @staticmethod
    def check_revision(symbol, expected):
        try:
            valid = int(expected) == symbol["revision"]
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise Conflict("状态已更新，请刷新页面后重试，避免覆盖新的行情或交易")

    @staticmethod
    def save_symbol(con, symbol, *, advance_revision=True):
        con.execute("UPDATE symbols SET name=?,config_json=?,state_json=?,revision=revision+?,updated_at=?,last_quote_epoch=? WHERE id=?",
                    (symbol["name"], dumps(symbol["config"]), dumps(symbol["state"]), int(advance_revision), stamp(),
                     symbol.get("last_quote_epoch", 0), symbol["id"]))
        if advance_revision:
            symbol["revision"] += 1

    @staticmethod
    def snapshot(con, symbol, kind, reason, event_id=None, at=None, *, price_override=None):
        seq = con.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE symbol_id=?", (symbol["id"],)).fetchone()[0]
        snapshot_state = dict(symbol["state"])
        # A small exception set captures branch membership, so undo then redo keeps
        # the audit flags consistent without duplicating the full ledger each frame.
        snapshot_state["_ledger_voided_ids"] = [r[0] for r in con.execute(
            "SELECT id FROM events WHERE symbol_id=? AND reverted_by IS NOT NULL", (symbol["id"],))]
        cur = con.execute("INSERT INTO snapshots(symbol_id,time,kind,reason,price,config_json,state_json,event_id,ledger_seq) VALUES(?,?,?,?,?,?,?,?,?)",
                          (symbol["id"], at or stamp(), kind, reason,
                           str(price_override) if price_override is not None else symbol["state"].get("market", {}).get("price"),
                           dumps(symbol["config"]), dumps(snapshot_state), event_id, seq))
        return cur.lastrowid

    @staticmethod
    def event(con, event_id, symbol_id, kind, title, detail, side=None, lots=None, price=None, at=None):
        con.execute("INSERT INTO events(id,symbol_id,time,kind,title,detail,side,lots,price) VALUES(?,?,?,?,?,?,?,?,?)",
                    (event_id, symbol_id, at or stamp(), kind, title, detail, side, lots, price))

    @staticmethod
    def prune_outbox(con, limit=PUSH_LOG_LIMIT):
        """Delete older resolved push logs; never discard an unsent message.

        Once the pending/sending backlog drains, the outbox contains at most
        ``limit`` rows. Keep latest rows by increasing id, rather than time,
        because dates can repeat across multi-part daily snapshots.
        This does not touch immutable trade events or snapshots.
        """
        con.execute(
            "DELETE FROM outbox WHERE id NOT IN "
            "(SELECT id FROM outbox ORDER BY id DESC LIMIT ?) "
            "AND status NOT IN ('pending','sending')",
            (limit,),
        )

    @staticmethod
    def enqueue(con, event_id, title, body, kind, symbol_id=None, force=False):
        # Keep ntfy text below its attachment threshold. UTF-8-safe chunks.
        parts, current = [], ""
        body = quant_style_message(title, body, kind)
        if kind == "daily" and str(body).startswith("🎯每日快照"):
            # Keep a whole contract together and repeat the short caption on
            # every message if monitoring many contracts. No transport title,
            # time suffix or (1/2) suffix; never slice a symbol in half.
            blocks = str(body).split("\n\n")
            caption = blocks[0]
            current = caption
            for block in blocks[1:]:
                addition = "\n\n" + block
                if len((current + addition).encode("utf-8")) > 3200 and current != caption:
                    parts.append(current)
                    current = caption
                if len((current + addition).encode("utf-8")) > 3200:
                    # Defensive fallback for an unusually long single block.
                    raise ValueError("单标的快照内容过长，不能安全推送")
                current += addition
            parts.append(current)
        else:
            for line in str(body).splitlines(keepends=True):
                while len(line.encode("utf-8")) > 3200:
                    piece = line[:900]
                    if current:
                        parts.append(current); current = ""
                    parts.append(piece); line = line[len(piece):]
                if len((current + line).encode("utf-8")) > 3200:
                    parts.append(current); current = ""
                current += line
            if current or not parts:
                parts.append(current)
        cfg = Store.setting(con, "push", PUSH_DEFAULTS)
        active = force or cfg.get("PUSH_ENABLED") == "yes"
        initial_status = "pending" if active else "disabled"
        initial_detail = "" if active else "触发时推送总开关关闭，记录保留，不自动补发"
        for i, part in enumerate(parts, 1):
            part_title = "" if kind == "daily" else (title if len(parts) == 1 else f"{title} ({i}/{len(parts)})")
            con.execute("INSERT OR IGNORE INTO outbox(event_id,part,symbol_id,time,title,body,kind,force,status,detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (event_id, i, symbol_id, stamp(), part_title, part, kind, int(force), initial_status, initial_detail))
        # Prune in the same transaction as the new message; never drop work.
        Store.prune_outbox(con)

    @staticmethod
    def request_seen(con, request_id, kind, symbol_id):
        import re
        if not re.fullmatch(r"[a-zA-Z0-9_-]{16,96}", str(request_id)):
            raise ValueError("操作标识失效，请刷新页面")
        row = con.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row:
            if row["kind"] != kind or row["symbol_id"] != symbol_id:
                raise ValueError("操作标识已用于其他操作")
            return loads(row["result"])
        return None

    @staticmethod
    def finish_request(con, request_id, kind, symbol_id, result):
        con.execute("INSERT INTO requests VALUES(?,?,?,?,?)", (request_id, kind, symbol_id, dumps(result), stamp()))

    @staticmethod
    def strategy_event_rows(con, symbol_id, limit=20):
        """Trade snapshot history: manual fills and triggered strategy signals.

        Both events are written transactionally at their original execution
        points. Read the existing event ledger without creating extra frames.
        Exclude configuration edits, ordinary quotes, monitoring alerts and
        rollback audit records. Keep reverted trades visible for verification.
        """
        return [dict(row) for row in con.execute(
            "SELECT * FROM events WHERE symbol_id=? AND kind IN ('signal','manual') "
            "ORDER BY seq DESC LIMIT ?", (symbol_id, limit))]

    @staticmethod
    def snapshot_rows(con, symbol_id, limit=50):
        result = []
        # Rollback history is event-driven, not one frame per price update.
        # Retain legacy `before_trade` snapshots as valid pre-fill checkpoints;
        # legacy after-event `manual`/`signal` rows remain in the DB for audit
        # and backwards-compatible backups but are never shown as pre-state.
        for row in con.execute(
            "SELECT s.*, e.reverted_by AS trigger_reverted_by, "
            "e.id AS trigger_exists FROM snapshots AS s "
            "LEFT JOIN events AS e ON e.id=s.event_id AND e.symbol_id=s.symbol_id "
            "WHERE s.symbol_id=? AND s.kind IN ('before_trade','before_signal') "
            "ORDER BY s.id DESC LIMIT ?", (symbol_id, limit)):
            item = dict(row)
            item["config"] = loads(item.pop("config_json"))
            item["state"] = loads(item.pop("state_json"))
            if item["event_id"]:
                item["can_rollback"] = bool(item["trigger_exists"] and not item["trigger_reverted_by"])
            else:
                # Legacy pre-fill frames have no recorded fill price; their
                # old market quote must not masquerade as a trade price.
                item["price"] = None
                # Legacy pre-fill checkpoints were created without an event id.
                item["can_rollback"] = con.execute(
                    "SELECT 1 FROM events WHERE symbol_id=? AND seq>? "
                    "AND kind IN ('manual','signal') AND reverted_by IS NULL LIMIT 1",
                    (symbol_id, item["ledger_seq"])
                ).fetchone() is not None
            result.append(item)
        return result

