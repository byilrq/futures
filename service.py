"""User actions: validate and commit state/event/snapshot/outbox together."""
from __future__ import annotations

import copy
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation

from market_data import SOURCE_ORDER
from config import as_bool, default_symbol_config, parse_time, stamp
from storage import Store, dumps, loads
from push_format import manual_trade_body
from strategy import initial_state, initial_portfolio, validate_config, apply_manual, decimal_value

SIDES = {"open_long": "买入开多", "close_long": "卖出平多", "open_short": "卖出开空", "close_short": "买入平空"}


def empty_strategy():
    return {"last_price": None, "next_buy": None, "next_sell": None,
            "buy_count": 0, "sell_count": 0, "cycle_id": uuid.uuid4().hex,
            "needs_baseline": True, "last_quote_at": None, "last_source": None}


def is_configured(cfg):
    return all(str(cfg.get(k) or "").strip() for k in ("lower_price", "upper_price", "down_step", "up_step"))


def positive(value, label, allow_zero=False):
    try:
        result = decimal_value(value, label)
        if not result.is_finite() or (result < 0 if allow_zero else result <= 0):
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label}须为{'非负' if allow_zero else '正'}数") from exc
    return result


def validate_symbol(values, previous=None):
    cfg = {**(previous or {}), **values}
    # Compatibility field only: all contracts obey the single system master switch.
    cfg["enabled"] = True
    cfg["source"] = str(cfg.get("source", "inherit"))
    if cfg["source"] not in ("inherit", "auto", *SOURCE_ORDER):
        raise ValueError("无效的标的行情源")
    cfg["night_end"] = str(cfg.get("night_end", "")).strip()
    if cfg["night_end"] and cfg["night_end"] != "off":
        parse_time(cfg["night_end"])
    if cfg.get("rearm_mode", "return") not in {"return", "manual"}:
        raise ValueError("无效的阶梯重启模式")
    cfg["monitor_step"] = str(positive(cfg.get("monitor_step", "100"), "价格监控间隔"))
    cfg["multiplier"] = str(positive(cfg.get("multiplier"), "合约乘数"))
    cfg["tick_size"] = str(positive(cfg.get("tick_size"), "最小变动价位"))
    if positive(cfg["monitor_step"], "价格监控间隔") % Decimal(cfg["tick_size"]):
        raise ValueError("价格监控间隔须为最小变动价位的整数倍")
    if not is_configured(cfg):
        from config import integer
        cfg["signal_lots"] = integer(cfg.get("signal_lots", 1), "每档提示手数", 1, 100000)
        return cfg
    validated = validate_config(cfg)
    cfg.update(validated)
    return cfg


def add_symbol(store, contract, name=""):
    name = str(name or contract.get("name") or contract["symbol"]).strip()
    if not name or len(name) > 80:
        raise ValueError("标的名称须为 1–80 个字符")
    cfg = default_symbol_config(contract)
    state = {"strategy": empty_strategy(), "portfolio": initial_portfolio(), "monitor": {"anchor": None},
             "market": {"price": None, "quote_at": None, "source": None, "status": "waiting", "error": "等待行情"},
             "last_reason": "标的已添加，请设置价格区间；全局策略开关统一控制所有已配置合约"}
    try:
        with store.tx() as con:
            cur = con.execute("INSERT INTO symbols(symbol,name,exchange,contract_json,config_json,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                              (contract["symbol"], name, contract["exchange"], dumps(contract), dumps(cfg), dumps(state), stamp(), stamp()))
            symbol = store.symbol(con, cur.lastrowid)
            # The rollback list contains trade/strategy events only.
            return symbol["id"]
    except sqlite3.IntegrityError as exc:
        raise ValueError("该合约已存在，请直接选择现有标的") from exc


def order_symbols_for_display(symbols, selected_id, saved_order=None, promote=False):
    """Pin selected contract; remaining options follow recent selection order."""
    valid_ids = {str(item["id"]) for item in symbols}
    raw = saved_order if isinstance(saved_order, list) else []
    recent = []
    for entry in raw:
        key = str(entry)
        if key in valid_ids and key not in recent:
            recent.append(key)
    selected = str(selected_id) if selected_id is not None else None
    if selected in valid_ids and (promote or selected not in recent):
        recent = [selected] + [value for value in recent if value != selected]
    rank = {key: idx for idx, key in enumerate(recent)}
    ordered = sorted(symbols, key=lambda item: (
        str(item["id"]) != selected,
        rank.get(str(item["id"]), len(rank) + int(item["id"]))
    ))
    return ordered, recent[:100]


def delete_symbol(store, symbol_id, expected_symbol, revision):
    """Permanently delete only the selected contract and its linked records.

    An explicit dialog protects this destructive operation at the UI layer;
    the symbol code and revision are checked again in the same SQLite tx to
    protect against stale tabs or concurrent manual trades.
    """
    with store.tx() as con:
        symbol = store.symbol(con, symbol_id)
        if str(expected_symbol).lower() != symbol["symbol"].lower():
            raise ValueError("标的已变化，请刷新页面后重新确认删除")
        store.check_revision(symbol, revision)
        counts = {}
        for table in ("source_quotes", "price_history", "snapshots", "events",
                      "outbox", "requests", "backtests"):
            cursor = con.execute(f"DELETE FROM {table} WHERE symbol_id=?", (symbol_id,))
            counts[table] = cursor.rowcount
        # Cancel pending manual quote-refresh requests for this contract.
        for row in con.execute("SELECT id,result FROM jobs WHERE kind='refresh' AND status='pending'"):
            data = loads(row["result"], {})
            if isinstance(data, dict) and data.get("symbol_id") == symbol_id:
                con.execute("UPDATE jobs SET status='error',result='标的已删除，取消行情刷新' WHERE id=?", (row["id"],))
        con.execute("DELETE FROM symbols WHERE id=?", (symbol_id,))
        return {"symbol": symbol["symbol"], "name": symbol["name"], "deleted": counts}


def save_config(store, symbol_id, values, revision):
    with store.tx() as con:
        symbol = store.symbol(con, symbol_id)
        store.check_revision(symbol, revision)
        cfg = validate_symbol(values, symbol["config"])
        old = symbol["config"]
        portfolio = symbol["state"]["portfolio"]
        if Decimal(cfg["multiplier"]) != Decimal(old["multiplier"]) and (portfolio["long_lots"] or portfolio["short_lots"]):
            raise ValueError("仍有持仓，不能更改合约乘数；请先核实并处理持仓记录")
        keys = ("lower_price", "upper_price", "down_step", "up_step", "signal_lots", "tick_size", "rearm_mode")
        reset = any(str(cfg.get(k)) != str(old.get(k)) for k in keys)
        # Configuration changes remain audit events but do not add rollback frames.
        if reset:
            symbol["state"]["strategy"] = initial_state(cfg) if is_configured(cfg) else empty_strategy()
        if reset or cfg["source"] != old["source"]:
            symbol["state"]["strategy"]["needs_baseline"] = True
            symbol["state"]["monitor"]["anchor"] = None
        if cfg["source"] != old["source"]:
            # An explicit operator source change starts a new sticky route.
            symbol["state"].pop("source_route", None)
        if cfg["monitor_step"] != old["monitor_step"]:
            symbol["state"]["monitor"]["anchor"] = None
        symbol["config"] = cfg
        reason = "参数已保存；价格阶梯已重置，下一条新行情建立基线" if reset else "参数已保存"
        symbol["state"]["last_reason"] = reason
        event_id = uuid.uuid4().hex
        store.event(con, event_id, symbol_id, "config", "修改标的参数", reason)
        store.save_symbol(con, symbol)
        return {"reason": reason, "revision": symbol["revision"]}


def manual_fill(store, symbol_id, values):
    with store.tx() as con:
        prior = store.request_seen(con, values.get("request_id"), "manual", symbol_id)
        if prior is not None:
            return {**prior, "duplicate": True}
        symbol = store.symbol(con, symbol_id)
        store.check_revision(symbol, values.get("revision"))
        side = values.get("side")
        if side not in SIDES:
            raise ValueError("请选择开多、平多、开空或平空")
        price = positive(values.get("price"), "成交价格")
        if price % Decimal(symbol["config"]["tick_size"]):
            raise ValueError("成交价须符合该合约的最小变动价位")
        # Manual fills intentionally never charge fees or accept notes from
        # legacy forms/API callers. History remains unchanged after upgrade.
        after = apply_manual(symbol["state"]["portfolio"], side, str(price), values.get("lots"), "0", symbol["config"]["multiplier"])
        from config import integer
        lots = integer(values.get("lots"), "成交手数", 1, 100000000)
        event_id = uuid.uuid4().hex
        # Snapshot the state BEFORE this trade, in the SAME transaction.
        # The snapshot ledger_seq excludes this trade event, so rollback
        # really removes this fill (and every later fill), not just later ones.
        snapshot_id = store.snapshot(con, symbol, "before_trade",
                                     f"{SIDES[side]} {lots} 手 @ {price}（成交前）",
                                     event_id=event_id, price_override=price)
        symbol["state"]["portfolio"] = after
        title = f"手动交易 · {symbol['name']} {symbol['symbol']}"
        traded_at = stamp()
        detail = (f"{SIDES[side]} {lots} 手，成交价 {price}\n"
                  f"多仓 {after['long_lots']} 手，均价 {after['long_avg']}；空仓 {after['short_lots']} 手，均价 {after['short_avg']}\n"
                  f"累计已实现损益 {after['realized_pnl']} 元\n记录时间：{traded_at}\n记录编号：{event_id[:12]}")
        store.event(con, event_id, symbol_id, "manual", title, detail, side, lots, str(price))
        con.execute("UPDATE events SET snapshot_id=? WHERE id=?", (snapshot_id, event_id))
        # Every recorded fill generates a push-outbox entry. The system's
        # global push switch still determines whether it is delivered.
        store.enqueue(con, event_id, title,
                      manual_trade_body(symbol, SIDES[side], lots, price, after, traded_at),
                      "manual", symbol_id)
        symbol["state"]["last_reason"] = f"已记录{SIDES[side]} {lots} 手；行情策略阶梯不变"
        store.save_symbol(con, symbol)
        result = {"event_id": event_id, "snapshot_id": snapshot_id}
        store.finish_request(con, values["request_id"], "manual", symbol_id, result)
        return result


def rollback(store, symbol_id, snapshot_id, revision, request_id):
    with store.tx() as con:
        prior = store.request_seen(con, request_id, "rollback", symbol_id)
        if prior is not None:
            return {**prior, "duplicate": True}
        symbol = store.symbol(con, symbol_id)
        store.check_revision(symbol, revision)
        row = con.execute("SELECT * FROM snapshots WHERE id=? AND symbol_id=?", (snapshot_id, symbol_id)).fetchone()
        if row is None or row["kind"] not in ("before_trade", "before_signal"):
            raise ValueError("只能回滚到手动交易或策略信号发生前的状态")
        if row["event_id"]:
            related = con.execute("SELECT reverted_by FROM events WHERE id=? AND symbol_id=?",
                                  (row["event_id"], symbol_id)).fetchone()
            if related is None:
                raise ValueError("对应交易或策略信号不存在，无法回滚")
            if related["reverted_by"] is not None:
                raise ValueError("该交易或策略信号已撤销，不能重复回滚")
        else:
            # Older versions created an unlinked 'before_trade' frame before
            # the fill. Only expose it while it can still undo a live event.
            live = con.execute(
                "SELECT 1 FROM events WHERE symbol_id=? AND seq>? "
                "AND kind IN ('manual','signal') AND reverted_by IS NULL LIMIT 1",
                (symbol_id, row["ledger_seq"])
            ).fetchone()
            if live is None:
                raise ValueError("该旧版成交前快照没有可撤销的后续成交或信号")
        event_id = uuid.uuid4().hex
        # Immutable quote watermark stays in symbols.last_quote_epoch, never rolled back.
        market = copy.deepcopy(symbol["state"]["market"])
        # An event rollback must NOT silently undo later configuration edits.
        # Keep current price bands, source, multiplier etc. even if a stored
        # checkpoint was created under another configuration.
        current_config = copy.deepcopy(symbol["config"])
        current_routing = copy.deepcopy(symbol["state"].get("source_route"))
        current_outage = copy.deepcopy(symbol["state"].get("source_outage"))
        checkpoint_config = loads(row["config_json"], {})
        symbol["state"] = loads(row["state_json"])
        # Rolling back a trade never rewinds operational source failover or
        # its one-alert-per-outage latch; those are unrelated to positions.
        if current_routing is not None:
            symbol["state"]["source_route"] = current_routing
        if current_outage is not None:
            symbol["state"]["source_outage"] = current_outage
        else:
            symbol["state"].pop("source_outage", None)
        voided = set(symbol["state"].pop("_ledger_voided_ids", []))
        symbol["state"]["market"] = market
        if checkpoint_config != current_config:
            # Restoring an old ladder under new thresholds would be invalid.
            symbol["state"]["strategy"] = (
                initial_state(current_config) if is_configured(current_config)
                else empty_strategy()
            )
        symbol["state"]["strategy"]["needs_baseline"] = True
        symbol["state"]["monitor"] = {"anchor": None}
        symbol["state"]["last_reason"] = f"已回滚到快照 #{snapshot_id}，下一条新行情建立基线"
        # Only manual fills and strategy signals are reverted.  Configuration
        # changes, source-monitor observations and system actions remain valid
        # audit facts and must not appear to be undone by a position rollback.
        for event in con.execute(
            "SELECT id,seq,reverted_by FROM events "
            "WHERE symbol_id=? AND kind IN ('manual','signal')", (symbol_id,)
        ).fetchall():
            active = event["seq"] <= row["ledger_seq"] and event["id"] not in voided
            con.execute("UPDATE events SET reverted_by=? WHERE id=?", (None if active else event_id, event["id"]))
            if not active:
                con.execute("UPDATE outbox SET status='cancelled',detail='已由状态回滚取消' WHERE event_id=? AND status IN ('pending','failed','disabled')", (event["id"],))
        detail = (f"已恢复到事件 #{snapshot_id}（{row['time']}）发生前：{row['reason']}。"
                  "该笔及其后续交易/信号已标记撤销，已发送通知保留；下一条新行情仅建立基线。")
        store.event(con, event_id, symbol_id, "rollback", "状态回滚", detail)
        # A rollback itself is an audit EVENT, not another rollback frame.
        store.save_symbol(con, symbol)
        store.enqueue(con, event_id, f"状态回滚 · {symbol['name']}", detail, "rollback", symbol_id)
        result = {"snapshot_id": int(snapshot_id), "event_id": event_id}
        store.finish_request(con, request_id, "rollback", symbol_id, result)
        return result


def reset_cycle(store, symbol_id, revision, request_id):
    with store.tx() as con:
        prior = store.request_seen(con, request_id, "reset", symbol_id)
        if prior is not None:
            return {**prior, "duplicate": True}
        symbol = store.symbol(con, symbol_id)
        store.check_revision(symbol, revision)
        # Explicit cycle resets do not create trade/trigger rollback rows.
        symbol["state"]["strategy"] = initial_state(symbol["config"]) if is_configured(symbol["config"]) else empty_strategy()
        symbol["state"]["monitor"]["anchor"] = None
        symbol["state"]["last_reason"] = "策略周期已重置，下一条新行情建立基线"
        event_id = uuid.uuid4().hex
        store.event(con, event_id, symbol_id, "reset", "重置策略周期", symbol["state"]["last_reason"])
        store.save_symbol(con, symbol)
        result = {"event_id": event_id}
        store.finish_request(con, request_id, "reset", symbol_id, result)
        return result
