"""One background worker; page reads never fetch prices or evaluate strategies."""
from __future__ import annotations

import argparse
import copy
import fcntl
import logging
import signal
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from decimal import Decimal

from config import BASE_DIR, SYSTEM_DEFAULTS, now_shanghai, parse_timestamp, session_info, stamp
from market_data import collect_quotes
from storage import Store, dumps, loads
from strategy import evaluate
from service import is_configured
from push import drain_outbox, recover_inflight
from push_format import daily_snapshot_body, signal_trade_body

LOG = logging.getLogger("futures")


def decorate_quotes(quotes, system, at):
    decorated = []
    for raw in quotes:
        q = dict(raw)
        q["fresh"] = False
        q["epoch"] = None
        q["selected"] = False
        if q.get("ok") and q.get("price") is not None:
            try:
                price = Decimal(str(q["price"]))
                if not price.is_finite() or price <= 0:
                    raise ValueError("无效行情价格")
                qt = parse_timestamp(q.get("quote_at"))
                age = (at - qt).total_seconds()
                q["fresh"] = -15 <= age <= system["quote_max_age"]
                q["epoch"] = qt.timestamp()
                q["price"] = str(price)
                q["quote_at"] = stamp(qt)
                if not q["fresh"]:
                    q["error"] = "行情时间在未来，已拒绝" if age < -15 else f"行情已过期（距今 {int(age)} 秒）"
            except (ValueError, ArithmeticError):
                q["ok"] = False
                q["error"] = "行情价格或时间无效"
        decorated.append(q)
    return decorated


def choose_quote(quotes, system, cfg):
    source = cfg.get("source", "inherit")
    source = system["quote_source"] if source == "inherit" else source
    usable = [q for q in quotes if q.get("ok") and q.get("fresh")]
    if source != "auto":
        chosen = next((q for q in usable if q["source"] == source), None)
        return chosen, "" if chosen else "指定行情源暂不可用/报价过期；未切换其他源"
    if len(usable) > 1:
        # Compare only similarly timed prices; older quotes cannot veto a fresh one.
        a, b = usable[:2]
        if abs(a["epoch"] - b["epoch"]) <= 10:
            limit = Decimal(str(cfg["tick_size"])) * system["source_spread_ticks"]
            if abs(Decimal(a["price"]) - Decimal(b["price"])) > limit:
                return None, "两源同时刻价差超过设定上限，暂停本轮判断"
    if not usable:
        return None, "两路行情暂不可用或已过期"
    # Sina primary; materially newer Eastmoney takes over. Source changes rebaseline.
    usable.sort(key=lambda q: (q["epoch"], q["source"] == "sina"), reverse=True)
    newest = usable[0]
    primary = next((q for q in usable if q["source"] == "sina"), None)
    if primary and newest["epoch"] - primary["epoch"] <= 5:
        return primary, ""
    return newest, ""


def process_quotes(store, symbol_id, raw_quotes, at=None):
    """Re-read configuration under one write transaction after network I/O."""
    at = at or now_shanghai()
    with store.tx() as con:
        symbol = store.symbol(con, symbol_id)
        system = {**SYSTEM_DEFAULTS, **store.setting(con, "system", {})}
        cfg, state = symbol["config"], symbol["state"]
        before_signal_state = copy.deepcopy(symbol)
        quotes = decorate_quotes(raw_quotes, system, at)
        # Wrong-contract responses never reach either the display price or strategy.
        for q in quotes:
            if str(q.get("symbol", "")).lower() != symbol["symbol"].lower():
                q.update(ok=False, fresh=False, error="行情合约与所监测合约不一致")
        chosen, error = choose_quote(quotes, system, cfg)
        info = session_info(system, at, cfg, symbol["contract"])
        active = bool(system["strategy_enabled"] and is_configured(cfg))
        if not active:
            state["strategy"]["needs_baseline"] = True
            state["monitor"]["anchor"] = None
        for q in quotes:
            q["selected"] = bool(chosen and q["source"] == chosen["source"])
            con.execute("INSERT INTO source_quotes VALUES(?,?,?,?) ON CONFLICT(symbol_id,source) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                        (symbol_id, q["source"], dumps(q), stamp(at)))
        if chosen is None:
            state["market"]["status"] = "stale" if any(q.get("ok") for q in quotes) else "error"
            state["market"]["error"] = error
            # Show the latest returned old price on first startup, labelled as stale.
            valid = [q for q in quotes if q.get("ok") and q.get("price")]
            if state["market"].get("price") is None and valid:
                old = max(valid, key=lambda q: q.get("epoch") or 0)
                state["market"].update(price=old["price"], quote_at=old.get("quote_at"), source=old["source"])
            state["last_reason"] = error
            store.save_symbol(con, symbol, advance_revision=False)
            return {"signals": [], "status": "unavailable"}
        if not info["open"] or not session_info(system, parse_timestamp(chosen["quote_at"]), cfg, symbol["contract"])["open"]:
            state["market"] = {"price": chosen["price"], "quote_at": chosen["quote_at"], "source": chosen["source"],
                               "status": "closed", "error": ""}
            state["last_reason"] = "非监测时段报价，仅展示；保留上次交易时段策略基线，开盘跳空正常判断"
            store.save_symbol(con, symbol, advance_revision=False)
            return {"signals": [], "status": "closed"}
        if chosen["epoch"] <= symbol["last_quote_epoch"]:
            state["market"].update(status="ok" if info["open"] else "closed", error="")
            store.save_symbol(con, symbol, advance_revision=False)
            return {"signals": [], "status": "duplicate"}
        market = {"price": chosen["price"], "quote_at": chosen["quote_at"], "source": chosen["source"],
                  "status": "ok" if info["open"] else "closed", "error": ""}
        previous_source = state["strategy"].get("last_source")
        source_changed = previous_source is not None and previous_source != chosen["source"]
        allowed = active
        signals, reason = [], ("策略总开关已关闭，仅更新行情" if not system["strategy_enabled"]
                               else "等待配置买卖价格线，仅更新行情")
        if not info["open"]:
            reason = "非监测时段，仅展示行情，不触发策略"
        if allowed:
            try:
                next_state, signals, reason = evaluate(cfg, state["strategy"], chosen["price"],
                    baseline=source_changed or bool(state["strategy"].get("needs_baseline", True)))
            except ValueError as exc:
                state["market"] = {**market, "status": "rejected", "error": str(exc)}
                state["last_reason"] = f"本轮行情未进入策略：{exc}"
                # No frontier, watermark or price history advance on extreme gaps.
                store.save_symbol(con, symbol, advance_revision=False)
                return {"signals": [], "status": "rejected"}
            state["strategy"] = next_state
            if source_changed:
                reason = f"行情源切换为 {chosen['source']}，已重新建立基线；{reason}"
        else:
            state["strategy"]["needs_baseline"] = True
        state["strategy"]["last_source"] = chosen["source"]
        state["strategy"]["last_quote_at"] = chosen["quote_at"]
        state["market"] = market
        state["last_reason"] = reason
        symbol["last_quote_epoch"] = chosen["epoch"]
        con.execute("INSERT OR IGNORE INTO price_history(symbol_id,time,epoch,price,source) VALUES(?,?,?,?,?)",
                    (symbol_id, chosen["quote_at"], chosen["epoch"], chosen["price"], chosen["source"]))
        batch_id = uuid.uuid4().hex
        event_ids = []
        snapshot_id = None
        if signals:
            # One record per actual trigger batch (several crossed price
            # levels from one quote still share a single BEFORE checkpoint).
            snapshot_id = store.snapshot(
                con, before_signal_state, "before_signal",
                f"{('向下买入' if signals[0]['side'] == 'buy' else '向上卖出')} "
                f"{len(signals)} 档（触发前），观察价 {chosen['price']}",
                event_id=f"{batch_id}_0", at=stamp(at), price_override=chosen["price"])
        for index, item in enumerate(signals):
            event_id = f"{batch_id}_{index}"
            side_cn = "买入" if item["side"] == "buy" else "卖出"
            previous_price = before_signal_state["state"]["strategy"].get("last_price")
            direction = "向下" if item["side"] == "buy" else "向上"
            step = cfg["down_step"] if item["side"] == "buy" else cfg["up_step"]
            detail = (f"触发原因：价格从 {previous_price} {direction}击穿{side_cn}线 {item['level']}；"
                      f"当前 {chosen['price']}；阶梯间隔 {step} 点；"
                      f"提示 {item['lots']} 手（仅策略信号，未自动成交）；"
                      f"行情源 {chosen['source']}；报价时间 {chosen['quote_at']}")
            store.event(con, event_id, symbol_id, "signal", f"{side_cn}提示", detail, item["side"], item["lots"], chosen["price"], stamp(at))
            event_ids.append(event_id)
        if signals:
            sides = "/".join(dict.fromkeys("买入" if x["side"] == "buy" else "卖出" for x in signals))
            body = signal_trade_body(symbol, signals, market,
                state["strategy"].get("next_buy"), state["strategy"].get("next_sell"))
            # Link the envelope to the first signal so rollback cancels the batch atomically.
            store.enqueue(con, event_ids[0], f"{sides}提示 · {symbol['name']}", body, "signal", symbol_id)
        monitor = state.setdefault("monitor", {"anchor": None})
        if allowed:
            if source_changed or monitor.get("anchor") is None:
                monitor["anchor"] = chosen["price"]
            elif abs(Decimal(chosen["price"]) - Decimal(str(monitor["anchor"]))) >= Decimal(str(cfg["monitor_step"])):
                change = Decimal(chosen["price"]) - Decimal(str(monitor["anchor"]))
                eid = uuid.uuid4().hex
                detail = f"{symbol['symbol']} 价格 {chosen['price']}，较上次监控锚 {monitor['anchor']} 变化 {change:+} 点；行情 {chosen['source']} / {chosen['quote_at']}"
                store.event(con, eid, symbol_id, "monitor", "价格监控", detail, price=chosen["price"])
                store.enqueue(con, eid, f"价格监控 · {symbol['name']}", detail, "monitor", symbol_id)
                monitor["anchor"] = chosen["price"]
        elif not allowed:
            monitor["anchor"] = None
        for eid in event_ids:
            con.execute("UPDATE events SET snapshot_id=? WHERE id=?", (snapshot_id, eid))
        store.save_symbol(con, symbol, advance_revision=bool(signals))
        return {"signals": signals, "status": "accepted", "snapshot_id": snapshot_id}


def refresh_quote_display(store, symbol_id, raw_quotes, at=None):
    """Manual refresh updates visible quotes only, never strategy execution.

    Keep last_quote_epoch and strategy state unchanged so the next scheduled
    background poll still processes any new actual quote exactly once.
    """
    at = at or now_shanghai()
    with store.tx() as con:
        symbol = store.symbol(con, symbol_id)
        system = {**SYSTEM_DEFAULTS, **store.setting(con, "system", {})}
        quotes = decorate_quotes(raw_quotes, system, at)
        for quote in quotes:
            if str(quote.get("symbol", "")).lower() != symbol["symbol"].lower():
                quote.update(ok=False, fresh=False, error="行情合约与所监测合约不一致")
        chosen, error = choose_quote(quotes, system, symbol["config"])
        for quote in quotes:
            quote["selected"] = bool(chosen and quote["source"] == chosen["source"])
            con.execute("INSERT INTO source_quotes VALUES(?,?,?,?) "
                        "ON CONFLICT(symbol_id,source) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                        (symbol_id, quote["source"], dumps(quote), stamp(at)))
        market = symbol["state"]["market"]
        if chosen:
            last_quote_at = market.get("quote_at")
            try:
                old_epoch = parse_timestamp(last_quote_at).timestamp() if last_quote_at else 0
            except ValueError:
                old_epoch = 0
            if chosen["epoch"] >= old_epoch:
                active_session = (session_info(system, at, symbol["config"], symbol["contract"])["open"]
                                  and session_info(system, parse_timestamp(chosen["quote_at"]), symbol["config"], symbol["contract"])["open"])
                market.update(price=chosen["price"], quote_at=chosen["quote_at"],
                              source=chosen["source"], status="ok" if active_session else "closed", error="")
            result = {"status": "ok", "price": chosen["price"], "source": chosen["source"],
                      "quote_at": chosen["quote_at"]}
        else:
            if market.get("price") is None:
                market.update(status="stale" if any(q.get("ok") for q in quotes) else "error",
                              error=error)
            result = {"status": "unavailable", "error": error}
        # Keep strategy explanations and signal history untouched on manual refresh.
        store.save_symbol(con, symbol, advance_revision=False)
        return result


def perform_schedules(store, at=None):
    at = at or now_shanghai()
    day, hm = str(at.date()), at.strftime("%H:%M")
    with store.tx() as con:
        cfg = {**SYSTEM_DEFAULTS, **store.setting(con, "system", {})}
        if hm >= cfg["daily_snapshot_time"] and store.meta(con, "daily_snapshot_day") != day:
            symbols = store.symbols(con)
            # Daily notification continues, but no daily rollback frames.
            # Only the body carries the caption; no ntfy title header, no
            # duplicated timestamp beside the caption or per-message suffix.
            store.enqueue(con, f"daily_{day}", "", daily_snapshot_body(symbols), "daily")
            store.set_meta(con, "daily_snapshot_day", day)
        if cfg["backup_enabled"] and hm >= cfg["backup_time"] and store.meta(con, "backup_queued_day") != day:
            con.execute("INSERT INTO jobs(kind,time) VALUES('backup',?)", (stamp(at),))
            store.set_meta(con, "backup_queued_day", day)
        if hm >= cfg["backup_time"] and store.meta(con, "maintenance_day") != day:
            cutoff = stamp(at - timedelta(days=cfg["history_keep_days"]))
            con.execute("DELETE FROM price_history WHERE time<?", (cutoff,))
            # Legacy quote-only frames are safe to trim; event checkpoints
            # and old audit records are never automatically discarded.
            con.execute("DELETE FROM snapshots WHERE time<? AND kind='quote' AND event_id IS NULL", (cutoff,))
            con.execute("DELETE FROM login_limits WHERE until<?", (at.timestamp() - 86400,))
            store.set_meta(con, "maintenance_day", day)


def perform_jobs(store):
    with store.tx() as con:
        row = con.execute("SELECT * FROM jobs WHERE status='pending' "
                          "ORDER BY CASE WHEN kind='refresh' THEN 0 ELSE 1 END,id LIMIT 1").fetchone()
        if row is None:
            return
        job = dict(row)
        con.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    try:
        if job["kind"] == "backup":
            from backup import backup_data
            result = backup_data(store)
        elif job["kind"] == "refresh":
            data = loads(job["result"], {})
            with store.read() as con:
                contract = store.symbol(con, data["symbol_id"])["contract"]
            quotes = collect_quotes(contract)
            result = refresh_quote_display(store, data["symbol_id"], quotes)
        else:
            raise ValueError("未知后台任务")
        with store.tx() as con:
            con.execute("UPDATE jobs SET status='done',result=? WHERE id=?", (dumps(result), job["id"]))
    except Exception as exc:
        LOG.exception("后台任务失败")
        with store.tx() as con:
            con.execute("UPDATE jobs SET status='error',result=? WHERE id=?", (str(exc)[:1000], job["id"]))


def poll_markets(store, fetcher=collect_quotes):
    at = now_shanghai()
    with store.read() as con:
        symbols = store.symbols(con)
        system = {**SYSTEM_DEFAULTS, **store.setting(con, "system", {})}
        last_closed = store.meta(con, "last_closed_fetch", 0)
        previous = {s["id"]: con.execute("SELECT COUNT(*) FROM source_quotes WHERE symbol_id=?", (s["id"],)).fetchone()[0] for s in symbols}
    due = [s for s in symbols if session_info(system, at, s["config"], s["contract"])["open"]
           or not previous[s["id"]] or time.time() - last_closed >= 900]
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(fetcher, s["contract"]): s["id"] for s in due}
        for future in as_completed(pending):
            symbol_id = pending[future]
            try:
                process_quotes(store, symbol_id, future.result(), now_shanghai())
            except Exception:
                LOG.exception("合约 %s 行情处理失败", symbol_id)
    with store.tx() as con:
        if time.time() - last_closed >= 900:
            store.set_meta(con, "last_closed_fetch", time.time())
        store.set_meta(con, "heartbeat", {"time": stamp(), "status": "ok", "symbols": len(symbols), "polled": len(due)})


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(BASE_DIR))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = Store(args.root).initialize()
    lock = (store.data / "worker.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("已有 Futures 行情进程运行，本次退出，避免重复计算")
    stop = False
    def stopping(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    recover_inflight(store)
    with store.tx() as con:
        con.execute("UPDATE jobs SET status='error',result='上次进程中断；请重新备份' WHERE status='running'")
    next_poll = 0.0
    auxiliary = ThreadPoolExecutor(max_workers=2, thread_name_prefix="futures-aux")
    job_future = push_future = None
    while not stop:
        try:
            if time.monotonic() >= next_poll:
                poll_started = time.monotonic()
                poll_markets(store)
                with store.read() as con:
                    interval = store.setting(con, "system", SYSTEM_DEFAULTS)["refresh_seconds"]
                next_poll = poll_started + interval
            perform_schedules(store)
            if args.once:
                perform_jobs(store)
                drain_outbox(store)
            else:
                # Remote backups and slow receivers must not delay market sampling.
                # Exactly one task per lane; SQLite locks are held only for commits.
                if job_future is None or job_future.done():
                    finished, job_future = job_future, None
                    if finished is not None:
                        finished.result()
                    job_future = auxiliary.submit(perform_jobs, store)
                if push_future is None or push_future.done():
                    finished, push_future = push_future, None
                    if finished is not None:
                        try:
                            finished.result()
                        except Exception:
                            recover_inflight(store)
                            raise
                    push_future = auxiliary.submit(drain_outbox, store)
        except Exception as exc:
            LOG.exception("后台循环出错，将在下一轮继续")
            try:
                with store.tx() as con:
                    store.set_meta(con, "heartbeat", {"time": stamp(), "status": "error", "detail": str(exc)[:500]})
            except Exception:
                LOG.exception("无法写入运行状态")
        if args.once:
            break
        for _ in range(10):
            if stop:
                break
            time.sleep(0.5)
    auxiliary.shutdown(wait=True, cancel_futures=True)
    lock.close()


if __name__ == "__main__":
    main()
