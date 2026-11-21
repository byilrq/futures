"""Flask portal following quant's 标的 / 系统 / 回测 layout."""
from __future__ import annotations

import csv
import fcntl
import hmac
import io
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from datetime import timedelta, date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.security import check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from nginx_proxy import proxy_state

from config import (APP_NAME, BASE_DIR, SYSTEM_DEFAULTS, PUSH_DEFAULTS, backup_display_state,
                    as_bool, now_shanghai, parse_timestamp, session_info, stamp,
                    validate_system)
from contracts import normalize_contract, supported_products, contract_month_status
from storage import Store, Conflict, dumps, loads
from service import add_symbol as add_symbol_record, save_config, manual_fill, rollback as restore_snapshot, reset_cycle, is_configured, delete_symbol as delete_symbol_record, order_symbols_for_display
from strategy import unrealized, validate_config as validate_strategy
from continuous import download_contract_chain, starting_main_price, run_main_backtest
from strategy import decimal_text
from push import validate_config as validate_push


def create_app(root=None):
    store = Store(root).initialize()
    code_root = Path(__file__).resolve().parent
    app = Flask(__name__, template_folder=str(code_root / "templates"), static_folder=str(code_root / "static"))
    with store.read() as con:
        app.secret_key = store.meta(con, "secret_key")
    app.config.update(MAX_CONTENT_LENGTH=8 * 1024 * 1024,
                      SESSION_COOKIE_NAME="futures_session", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=as_bool(os.environ.get("FUTURES_COOKIE_SECURE", "0")),
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    proxy = proxy_state(store.root)
    if proxy.get("enabled"):
        # Only the private loopback backend is reachable through our managed Nginx.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
        app.config["SESSION_COOKIE_SECURE"] = proxy.get("https", False)
    app.extensions["futures_store"] = store
    portal_lock = (store.data / "portal.lock").open("a+")
    fcntl.flock(portal_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
    app.extensions["futures_portal_lock"] = portal_lock

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    app.jinja_env.globals.update(csrf_token=csrf_token, request_id=lambda: uuid.uuid4().hex)

    @app.template_filter("num")
    def num(value):
        if value is None or value == "":
            return "—"
        try:
            n = Decimal(str(value))
            if not n.is_finite():
                return "—"
            return f"{n:,.4f}".rstrip("0").rstrip(".")
        except (InvalidOperation, ValueError):
            return str(value)

    @app.template_filter("dt")
    def dt(value):
        if not value:
            return "—"
        try:
            return parse_timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return str(value)

    @app.template_filter("jsonpretty")
    def jsonpretty(value):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)

    def safe_next(default="/status"):
        target = request.form.get("next", default)
        if not target.startswith("/") or target.startswith("//") or "\\" in target or urlsplit(target).netloc:
            return default
        return target

    @app.before_request
    def authenticate():
        if request.endpoint in {"static", "health"}:
            return None
        with store.read() as con:
            auth = store.setting(con, "auth", {})
        if request.endpoint != "login":
            if not auth or session.get("auth_version") != auth.get("version") or session.get("login_method") != "password":
                if request.path.startswith("/api/"):
                    return jsonify(error="登录已失效", login=url_for("login")), 401
                return redirect(url_for("login"))
        if request.method == "POST":
            supplied = request.form.get("csrf_token", request.headers.get("X-CSRF-Token", ""))
            if not supplied or not hmac.compare_digest(supplied, session.get("csrf", "")):
                abort(400, "页面已过期或校验失败，请刷新后重试")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/health")
    def health():
        # No account/contract/configuration information in the public liveness probe.
        return jsonify(application="futures", status="ok")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        with store.read() as con:
            auth = store.setting(con, "auth", {})
        if request.method == "POST":
            if not auth:
                flash("尚未设置登录密码，请先使用 futures.sh 完成安装。", "error")
            else:
                key = request.remote_addr or "local"
                with store.read() as con:
                    row = con.execute("SELECT failures,until FROM login_limits WHERE key=?", (key,)).fetchone()
                blocked = bool(row and row["failures"] >= 8 and row["until"] > time.time())
                if blocked:
                    flash("登录失败次数较多，请 5 分钟后重试。", "error")
                else:
                    password = str(request.form.get("password", ""))[:1024]
                    password_ok = check_password_hash(auth["password_hash"], password)
                    if password_ok:
                        with store.tx() as con:
                            con.execute("DELETE FROM login_limits WHERE key=?", (key,))
                        session.clear()
                        session.update(login_method="password", auth_version=auth["version"], csrf=secrets.token_urlsafe(32))
                        session.permanent = True
                        return redirect(url_for("status"))
                    with store.tx() as con:
                        current = con.execute("SELECT failures,until FROM login_limits WHERE key=?", (key,)).fetchone()
                        failures = current["failures"] + 1 if current and current["until"] > time.time() else 1
                        con.execute("INSERT INTO login_limits VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET failures=excluded.failures,until=excluded.until", (key, failures, time.time() + 300))
                    flash("密码错误。", "error")
        return render_template("login.html", app_name=APP_NAME, setup_required=not bool(auth))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    def context(page="status", symbol_id=None):
        with store.read() as con:
            system = {**SYSTEM_DEFAULTS, **store.setting(con, "system", {})}
            symbols = store.symbols(con)
            selected_id = symbol_id or request.values.get("symbol_id") or session.get("selected_symbol_id")
            selected = next((s for s in symbols if str(s["id"]) == str(selected_id)), symbols[0] if symbols else None)
            # The current contract is always first; the remaining options
            # follow recent selection order saved in the browser session.
            saved_order = session.get("recent_symbol_ids", [])
            if selected:
                session["selected_symbol_id"] = selected["id"]
                promote = request.method == "GET" and request.args.get("symbol_id") is not None
                symbols, recent = order_symbols_for_display(symbols, selected["id"], saved_order, promote)
                if recent != saved_order:
                    session["recent_symbol_ids"] = recent
            heartbeat = store.meta(con, "heartbeat", {})
            backup_status = store.meta(con, "backup_status", {"status": "none", "time": None, "detail": "尚未备份"})
            backup_last_success = store.meta(con, "backup_last_success", {})
            backup_indicator = backup_display_state(system, backup_status, backup_last_success, at=now_shanghai())
            quotes, snapshots, events = [], [], []
            if selected:
                quotes = [loads(r[0]) for r in con.execute("SELECT payload FROM source_quotes WHERE symbol_id=? ORDER BY source DESC", (selected["id"],))]
                snapshots = store.snapshot_rows(con, selected["id"], limit=20)
                events = store.strategy_event_rows(con, selected["id"], limit=20)
            for source, label in (("sina", "新浪期货"), ("eastmoney", "东方财富")):
                if not any(q["source"] == source for q in quotes):
                    quotes.append(dict(source=source, label=label, price=None, quote_at=None, received_at=None, ok=False, fresh=False, selected=False, error="等待后台采集"))
            # Recompute freshness on read: a stopped worker cannot leave a green old card.
            current_time = now_shanghai()
            for q in quotes:
                try:
                    q["fresh"] = q.get("ok", False) and -15 <= (current_time - parse_timestamp(q.get("quote_at"))).total_seconds() <= system["quote_max_age"]
                    if q.get("ok") and not q["fresh"]:
                        q["error"] = "报价已过期，仅供查看"
                except ValueError:
                    q["fresh"] = False
            current = selected["state"]["market"].get("price") if selected else None
            if selected and selected["state"]["market"].get("quote_at"):
                try:
                    age = (current_time - parse_timestamp(selected["state"]["market"]["quote_at"])).total_seconds()
                    if age > system["quote_max_age"] or age < -15:
                        selected["state"]["market"]["status"] = "stale"
                        selected["state"]["market"]["error"] = "报价已过期，仅供查看"
                except ValueError:
                    selected["state"]["market"]["status"] = "error"
            if heartbeat.get("time"):
                try:
                    if (current_time - parse_timestamp(heartbeat["time"])).total_seconds() > max(180, 3 * system["refresh_seconds"]):
                        heartbeat = {**heartbeat, "status": "error", "detail": "后台心跳已超时，请检查行情服务"}
                except ValueError:
                    heartbeat = {**heartbeat, "status": "error", "detail": "心跳时间无效"}
            floating = unrealized(selected["state"]["portfolio"], current, selected["config"]["multiplier"]) if selected and current else None
            info = session_info(system, current_time, selected["config"] if selected else None, selected["contract"] if selected else None)
            return dict(page=page, app_name=APP_NAME, system=system, symbols=symbols,
                        selected=selected, now=stamp(current_time), server_now=current_time.astimezone(ZoneInfo(system["timezone"])).strftime("%Y-%m-%d %H:%M:%S %Z"),
                        heartbeat=heartbeat, backup_status=backup_status, backup_indicator=backup_indicator, quotes=quotes, snapshots=snapshots,
                        events=events, in_session=info["open"], session_label=info["label"],
                        unrealized=floating, contract_month=contract_month_status(selected["contract"], today=current_time.date()) if selected else None,
                        status_text=selected["state"].get("last_reason", "") if selected else "尚未添加合约")

    def action_response(message, destination, **payload):
        # HTML POST remains the no-JavaScript fallback. JS keeps the current
        # scroll position, panel state and field values without a redirect.
        if request.headers.get("X-Futures-Ajax") == "1":
            return jsonify(ok=True, message=message, **payload)
        flash(message, "success")
        return redirect(destination)

    def latest_revision(symbol_id):
        with store.read() as con:
            return store.symbol(con, symbol_id)["revision"]

    @app.get("/")
    def home():
        return redirect(url_for("status"))

    @app.get("/status")
    def status():
        return render_template("status.html", **context("status"))

    @app.get("/api/status")
    def api_status():
        ctx = context("status")
        symbol = ctx["selected"]
        # Selected values, not configuration secrets. Template fragment contains no forms.
        template = "_live.html"
        try:
            panel = render_template(template, **ctx)
        except Exception:
            app.logger.exception("状态片段渲染失败")
            panel = ""
        return jsonify(panel_html=panel, revision=symbol["revision"] if symbol else None,
                       refresh_seconds=ctx["system"]["refresh_seconds"], in_session=ctx["in_session"],
                       selected_id=symbol["id"] if symbol else None, quotes=ctx["quotes"],
                       state=symbol["state"] if symbol else None, heartbeat=ctx["heartbeat"])

    @app.post("/strategy/toggle")
    def toggle_strategy():
        with store.tx() as con:
            cfg = {**SYSTEM_DEFAULTS, **store.setting(con, "system", {})}
            enabled = as_bool(request.form.get("enabled"))
            if cfg["strategy_enabled"] != enabled:
                cfg["strategy_enabled"] = enabled
                store.set_setting(con, "system", cfg)
                for symbol in store.symbols(con):
                    symbol["state"]["strategy"]["needs_baseline"] = True
                    symbol["state"]["monitor"]["anchor"] = None
                    store.save_symbol(con, symbol)
                eid = uuid.uuid4().hex
                detail = ("策略总开关已手动开启，下一条有效新行情建立基线。" if enabled else
                          f"策略总开关已手动关闭\n时间：{stamp()}（上海时间）\n所有合约停止自动买卖信号和价格监控；行情继续采集。")
                store.event(con, eid, None, "settings", "策略总开关", detail)
                # Like Quant, the shutdown alert is sent once for a genuine
                # ON -> OFF transition. Force queueing even when the regular
                # push toggle is off; no second notification on replays.
                if not enabled:
                    store.enqueue(con, eid, "🔴 策略总开关已关闭", detail, "settings", force=True)
        return action_response("策略总开关已开启。" if enabled else "策略总开关已关闭，已记录关闭提醒。", safe_next(), strategy_enabled=enabled)

    @app.post("/symbols/add")
    def add_symbol():
        contract = normalize_contract(request.form.get("symbol", ""), request.form.get("exchange") or None)
        symbol_id = add_symbol_record(store, contract, request.form.get("name", ""))
        flash(f"已添加 {contract['symbol']}；请填写价格区间，总开关会自动控制这个合约。" + (contract.get("normalization_note") or ""), "success")
        return redirect(url_for("status", symbol_id=symbol_id))

    @app.post("/symbols/<int:symbol_id>/refresh")
    def refresh_symbol(symbol_id):
        # One explicit request; the already running market worker fetches both
        # sources. Web never starts a second strategy engine or blocks on HTTP.
        with store.tx() as con:
            symbol = store.symbol(con, symbol_id)
            queued = None
            for row in con.execute("SELECT id,result FROM jobs WHERE kind='refresh' AND status IN ('pending','running')"):
                job_data = loads(row["result"], {})
                if isinstance(job_data, dict) and job_data.get("symbol_id") == symbol_id:
                    queued = row["id"]
                    break
            if queued is None:
                cursor = con.execute("INSERT INTO jobs(kind,time,result) VALUES('refresh',?,?)",
                                     (stamp(), dumps({"symbol_id": symbol_id, "symbol": symbol["symbol"]})))
                queued = cursor.lastrowid
        return action_response(f"已请求刷新 {symbol['symbol']}，后台正在获取两路最新行情。",
                               url_for("status", symbol_id=symbol_id), refresh_job_id=queued)

    @app.get("/api/refresh/<int:job_id>")
    def refresh_job_status(job_id):
        with store.read() as con:
            row = con.execute("SELECT kind,status,result FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["kind"] != "refresh":
            abort(404)
        detail = loads(row["result"], {}) if row["status"] != "error" else row["result"]
        return jsonify(status=row["status"], detail=detail)

    @app.post("/symbols/<int:symbol_id>/delete")
    def delete_symbol(symbol_id):
        if request.form.get("confirm_delete") != "yes":
            raise ValueError("请在站内删除确认弹窗中确认本次操作")
        result = delete_symbol_record(store, symbol_id,
                                      request.form.get("symbol", ""),
                                      request.form.get("revision"))
        if str(session.get("selected_symbol_id")) == str(symbol_id):
            session.pop("selected_symbol_id", None)
        saved_order = session.get("recent_symbol_ids", [])
        if isinstance(saved_order, list):
            session["recent_symbol_ids"] = [value for value in saved_order if str(value) != str(symbol_id)]
        flash(f"已删除 {result['name']}（{result['symbol']}）及其关联运行记录。", "success")
        return redirect(url_for("status"), code=303)

    @app.post("/symbols/<int:symbol_id>/save")
    def save_symbol(symbol_id):
        values = {k: request.form[k] for k in ("lower_price", "upper_price", "down_step", "up_step", "signal_lots", "multiplier", "tick_size", "monitor_step", "night_end", "source", "rearm_mode") if k in request.form}
        result = save_config(store, symbol_id, values, request.form.get("revision"))
        return action_response(result["reason"], url_for("status", symbol_id=symbol_id),
                               symbol_id=symbol_id, revision=result["revision"])

    @app.post("/symbols/<int:symbol_id>/trade")
    def manual_trade(symbol_id):
        result = manual_fill(store, symbol_id, request.form.to_dict())
        return action_response("该交易已记录，重复提交未新增记录。" if result.get("duplicate") else f"交易已记录，可回滚至本次成交前（记录 #{result['snapshot_id']}）。",
                               url_for("status", symbol_id=symbol_id), symbol_id=symbol_id, revision=latest_revision(symbol_id), rotate_request_id=True, next_request_id=uuid.uuid4().hex)

    @app.post("/symbols/<int:symbol_id>/rollback")
    def rollback(symbol_id):
        result = restore_snapshot(store, symbol_id, request.form.get("snapshot_id"), request.form.get("revision"), request.form.get("request_id"))
        return action_response(f"已回滚到交易或信号触发前（记录 #{result['snapshot_id']}）；旧消息不会自动重发。",
                               url_for("status", symbol_id=symbol_id), symbol_id=symbol_id, revision=latest_revision(symbol_id), rotate_request_id=True, next_request_id=uuid.uuid4().hex)

    @app.post("/symbols/<int:symbol_id>/reset")
    def reset_strategy(symbol_id):
        reset_cycle(store, symbol_id, request.form.get("revision"), request.form.get("request_id"))
        return action_response("策略周期已重置；下一条新行情只建立观察基线。",
                               url_for("status", symbol_id=symbol_id), symbol_id=symbol_id, revision=latest_revision(symbol_id), rotate_request_id=True, next_request_id=uuid.uuid4().hex)

    @app.get("/system")
    @app.get("/push")
    def system_page():
        ctx = context("system")
        with store.read() as con:
            push = {**PUSH_DEFAULTS, **store.setting(con, "push", {})}
            logs = [dict(r) for r in con.execute("SELECT * FROM outbox WHERE hidden=0 ORDER BY id DESC LIMIT 30")]
        ctx.update(push_has_password=bool(push["NTFY_PASSWORD"]), push_has_token=bool(push["GOTIFY_TOKEN"]),
                   push_password_length=len(push["NTFY_PASSWORD"]))
        push["NTFY_PASSWORD"] = ""; push["GOTIFY_TOKEN"] = ""
        from backup import list_backups
        ctx.update(push=push, push_logs=logs, backup_files=list_backups(store), products=supported_products())
        return render_template("system.html", **ctx)

    @app.post("/system/settings")
    def save_system():
        with store.tx() as con:
            old = {**SYSTEM_DEFAULTS, **store.setting(con, "system", {})}
            values = {**old, **{k: request.form[k] for k in SYSTEM_DEFAULTS if k in request.form}}
            # The master strategy switch can ONLY be changed by the explicit
            # header action, never by a missing checkbox in settings POST.
            values["strategy_enabled"] = old["strategy_enabled"]
            for k in ("night_enabled", "backup_enabled"):
                values[k] = as_bool(request.form.get(k))
            cfg = validate_system(values)
            if cfg["quote_source"] != old["quote_source"]:
                for symbol in store.symbols(con):
                    symbol["state"]["strategy"]["needs_baseline"] = True
                    symbol["state"]["monitor"]["anchor"] = None
                    store.save_symbol(con, symbol)
            store.set_setting(con, "system", cfg)
            eid = uuid.uuid4().hex
            store.event(con, eid, None, "settings", "修改系统设置", "已保存运行时间、行情源、时区与备份设置")
        return action_response("系统设置已保存，后台下一轮生效；全部执行时间按上海时间。", url_for("system_page"))

    @app.post("/system/push")
    def save_push():
        with store.tx() as con:
            old = store.setting(con, "push", PUSH_DEFAULTS)
            values = request.form.to_dict()
            values["PUSH_ENABLED"] = "yes" if as_bool(values.get("PUSH_ENABLED")) else "no"
            cfg = validate_push(values, old)
            store.set_setting(con, "push", cfg)
            if cfg["PUSH_ENABLED"] != "yes":
                con.execute("UPDATE outbox SET status='disabled',detail='推送总开关已关闭，不自动补发' WHERE status='pending' AND force=0")
            store.prune_outbox(con)
        return action_response("推送配置已保存；留空的密码和 Token 已保持原值。", url_for("system_page"),
                               ntfy_password_length=len(cfg["NTFY_PASSWORD"]))

    @app.post("/system/push/test")
    def test_push():
        with store.tx() as con:
            store.enqueue(con, uuid.uuid4().hex, "闲云期货 · 推送测试", f"推送通道测试\n时间：{stamp()}（上海时间）", "test", force=True)
        return action_response("测试消息已排队，后台将在数秒内发送；结果见推送日志。", url_for("system_page"))

    @app.post("/system/push/clear")
    def clear_push_logs():
        with store.tx() as con:
            con.execute("UPDATE outbox SET hidden=1 WHERE status NOT IN ('pending','sending')")
        return action_response("已清空日志展示，交易和推送审计记录仍保留。", url_for("system_page"))

    @app.post("/system/push/<int:message_id>/retry")
    def retry_push(message_id):
        with store.tx() as con:
            row = con.execute("SELECT * FROM outbox WHERE id=?", (message_id,)).fetchone()
            if not row or row["status"] not in {"failed", "unknown"}:
                raise ValueError("仅失败或送达不确定的消息可以重试")
            event = con.execute("SELECT reverted_by FROM events WHERE id=?", (row["event_id"],)).fetchone()
            if event and event["reverted_by"]:
                raise ValueError("该信号或交易已回滚，不能重发")
            con.execute("UPDATE outbox SET status='pending',detail='用户明确请求重试',hidden=0 WHERE id=?", (message_id,))
        return action_response("已排队重试；如上次已送达，客户端可能再次收到该记录。", url_for("system_page"))

    @app.post("/system/backup")
    def request_backup():
        with store.tx() as con:
            pending = con.execute("SELECT 1 FROM jobs WHERE kind='backup' AND status IN ('pending','running')").fetchone()
            if not pending:
                con.execute("INSERT INTO jobs(kind,time) VALUES('backup',?)", (stamp(),))
        return action_response("备份任务已排队，完成后在页面顶部显示结果。", url_for("system_page"))

    def read_saved_backtest(payload, run_id=""):
        """Allow older report layouts, but never let one damaged report break the page.

        This only normalizes the in-memory rendering copy, never alters the
        historical result stored in SQLite.
        """
        try:
            report = loads(payload)
            if not isinstance(report, dict) or not isinstance(report.get("stats"), dict):
                raise ValueError("invalid backtest report structure")
            report = dict(report)
            report["stats"] = dict(report["stats"])
            for key in ("rolls", "signals", "trades", "series", "warnings"):
                if not isinstance(report.get(key), list):
                    report[key] = []
            for key in ("rolls", "signals", "trades", "series"):
                report[key] = [entry for entry in report[key] if isinstance(entry, dict)]
            report["warnings"] = [str(entry) for entry in report["warnings"]]
            config = report.get("config")
            config = dict(config) if isinstance(config, dict) else {}
            strategy = config.get("strategy")
            strategy = dict(strategy) if isinstance(strategy, dict) else {}
            strategy.setdefault("lower_price", None)
            strategy.setdefault("upper_price", None)
            config["strategy"] = strategy
            if config.get("mode") not in ("grid", "hold"):
                config["mode"] = "legacy"
                report["legacy"] = True
            report["config"] = config
            report["stats"].setdefault("rolls", len(report["rolls"]))
            if not isinstance(report.get("source_info"), dict):
                report["source_info"] = None
            else:
                source_info = dict(report["source_info"])
                if not isinstance(source_info.get("providers"), dict):
                    source_info["providers"] = {}
                source_info.setdefault("requested", 0)
                source_info.setdefault("received", 0)
                report["source_info"] = source_info
            return report
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            app.logger.warning("Skipping invalid archived backtest %s: %s", run_id, type(exc).__name__)
            return None

    @app.route("/backtest", methods=["GET", "POST"])
    def backtest_page():
        ctx = context("backtest")
        result = None
        if request.method == "POST":
            selected = ctx["selected"]
            if selected is None:
                raise ValueError("请先添加需要回测的合约")
            # Backtests ALWAYS fetch historical actual monthly contracts from
            # public providers. Nothing is read from local price_history.
            source = request.form.get("source", "auto")
            mode = request.form.get("mode", "grid")
            today = now_shanghai().date()
            start = request.form.get("start", "").strip() or (today - timedelta(days=730)).isoformat()
            end = request.form.get("end", "").strip() or today.isoformat()
            try:
                begin_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
            except ValueError as exc:
                raise ValueError("回测日期格式必须为 YYYY-MM-DD") from exc
            if begin_date > end_date or (end_date - begin_date).days > 366 * 5:
                raise ValueError("回测开始时间不能晚于结束时间，最长支持 5 年")
            if end_date > today:
                raise ValueError("回测结束日期不得晚于当前上海日期")
            if source not in ("auto", "sina", "eastmoney"):
                raise ValueError("回测必须直接选择免费历史行情来源：自动、新浪或东方财富")
            try:
                history = download_contract_chain(selected["contract"], start, end, source=source)
                first = starting_main_price(history, start, end)
            except (OSError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
                app.logger.exception("Remote contract history retrieval failed unexpectedly")
                raise ValueError("历史行情下载异常，请查看网页服务日志并尝试另一个数据源。") from exc
            cfg = dict(selected["config"])
            for key in ("down_step", "up_step", "signal_lots", "multiplier", "tick_size", "rearm_mode"):
                if request.form.get(key):
                    cfg[key] = request.form[key]
            # Automatically set thresholds around the first real main close:
            # useful even for a newly added symbol without configured live lines.
            if request.form.get("lines_mode", "auto") == "auto":
                from decimal import ROUND_FLOOR, ROUND_CEILING
                tick = Decimal(str(cfg['tick_size']))
                first_price = Decimal(first['price'])
                down = Decimal(str(cfg['down_step']))
                up = Decimal(str(cfg['up_step']))
                if down <= 0 or up <= 0 or tick <= 0:
                    raise ValueError('回测间隔与最小变动价位必须大于零')
                lower = max(tick, ((first_price-down)/tick).to_integral_value(rounding=ROUND_FLOOR)*tick)
                upper = ((first_price+up)/tick).to_integral_value(rounding=ROUND_CEILING)*tick
                cfg['lower_price'] = decimal_text(lower)
                cfg['upper_price'] = decimal_text(upper)
            else:
                for key in ('lower_price', 'upper_price'):
                    cfg[key] = request.form.get(key) or cfg.get(key)
            cfg = validate_strategy(cfg)
            options = {key: request.form[key] for key in (
                'initial_cash','max_lots','fee_per_lot','slippage','roll_days',
                'roll_confirm','oi_ratio','hold_lots') if request.form.get(key)}
            options.update(start=start,end=end,mode=mode,source=source)
            try:
                result = run_main_backtest(history, selected['contract'], cfg, options)
            except (ArithmeticError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
                app.logger.exception("Backtest engine failed; no new report was stored")
                raise ValueError("回测计算异常；没有保存本次结果，请查看服务日志。") from exc
            result['first_main'] = first
            result["id"] = uuid.uuid4().hex
            result["symbol"] = selected["symbol"]
            result["source"] = source
            with store.tx() as con:
                con.execute("INSERT INTO backtests VALUES(?,?,?,?,?)", (result["id"], selected["id"], stamp(), selected["symbol"], dumps(result)))
                # Keep the last 20 reports to bound disk use; live state is never touched.
                con.execute("DELETE FROM backtests WHERE id NOT IN (SELECT id FROM backtests ORDER BY time DESC,rowid DESC LIMIT 20)")
            flash("回测完成，未改变任何实时持仓或策略状态。", "success")
        elif request.args.get("run_id"):
            with store.read() as con:
                row = con.execute("SELECT id,result,symbol_id,symbol FROM backtests WHERE id=?", (request.args["run_id"],)).fetchone()
                if row:
                    result = read_saved_backtest(row["result"], row["id"])
                    if result is not None:
                        result.setdefault("id", row["id"])
                        result.setdefault("symbol", row["symbol"])
            if row and result is not None:
                ctx = context("backtest", symbol_id=row["symbol_id"])
            elif row:
                flash("旧回测报告数据异常，已跳过显示；实盘记录未改动。", "error")
        with store.read() as con:
            rows = con.execute("SELECT id,time,symbol,result FROM backtests ORDER BY time DESC LIMIT 20").fetchall()
        runs = []
        for r in rows:
            report = read_saved_backtest(r["result"], r["id"])
            if report is None:
                continue
            runs.append(dict(id=r["id"], time=r["time"], symbol=r["symbol"], summary=report["stats"]))
        default_end = now_shanghai().date()
        default_start = default_end - timedelta(days=730)
        ctx.update(result=result, runs=runs, form=request.form, defaults={
            "initial_cash": 100000,"max_lots": 20,"hold_lots": 1,
            "fee_per_lot": 1, "slippage": 1,
            "start":default_start.isoformat(), "end":default_end.isoformat(),
            "roll_days":15,"roll_confirm":2,"oi_ratio":"1.05"})
        return render_template("backtest.html", **ctx)

    @app.get("/backtest/<run_id>.csv")
    def download_backtest(run_id):
        with store.read() as con:
            row = con.execute("SELECT result FROM backtests WHERE id=?", (run_id,)).fetchone()
        if not row:
            abort(404)
        result = loads(row[0])
        stream = io.StringIO()
        fields = ["time", "symbol", "side", "action", "price", "lots", "fee", "realized_pnl", "equity", "reason"]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for trade in result.get("trades", []):
            safe = {k: ("'" + str(v) if isinstance(v, str) and v.startswith(("=", "+", "@")) else v) for k, v in trade.items()}
            writer.writerow(safe)
        return send_file(io.BytesIO(stream.getvalue().encode("utf-8-sig")), mimetype="text/csv", as_attachment=True, download_name=f"futures_backtest_{run_id[:8]}.csv")

    @app.errorhandler(Conflict)
    def conflict_error(error):
        if request.headers.get("X-Futures-Ajax") == "1":
            return jsonify(ok=False, error=str(error)), 409
        flash(str(error), "error")
        return redirect(safe_next(request.referrer_path if hasattr(request, "referrer_path") else "/status")), 303

    @app.errorhandler(ValueError)
    def validation_error(error):
        if request.path.startswith("/api/") or request.headers.get("X-Futures-Ajax") == "1":
            return jsonify(ok=False, error=str(error)), 400
        flash(str(error), "error")
        destination = "/backtest" if request.path.startswith("/backtest") else "/system" if request.path.startswith("/system") or request.path == "/symbols/add" else "/status"
        return redirect(destination), 303

    @app.errorhandler(sqlite3.Error)
    def database_error(error):
        app.logger.exception("数据库操作失败")
        if request.headers.get("X-Futures-Ajax") == "1":
            return jsonify(ok=False, error="数据库暂时不可写，本次操作未提交。"), 503
        return "数据库暂时不可写，本次操作未提交。请检查磁盘空间和服务日志后重试。", 503

    @app.errorhandler(413)
    def large_upload(error):
        return "文件超过 8 MiB，请缩小 CSV 样本。", 413

    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(BASE_DIR))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8197)
    args = parser.parse_args()
    create_app(args.root).run(host=args.host, port=args.port, debug=False)
