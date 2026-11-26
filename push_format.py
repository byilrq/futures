"""Compact Quant-like notification bodies; transport titles stay optional.

Daily snapshots deliberately have NO ntfy/Gotify title. The only caption is
"🎯每日快照" inside the body. Quote timestamp is shown on its own line, never
appended to that caption. Actual fills and strategy SIGNALS are distinct.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal

from strategy import decimal_text, decimal_value, unrealized
from market_data import SOURCE_LABELS, source_options
from config import SHANGHAI, SYSTEM_DEFAULTS, parse_time, parse_timestamp, session_info

PREFIXES = {
    "signal": "🎯[TRADE]", "manual": "🎯[TRADE]", "trade": "🎯[TRADE]",
    "monitor": "🏹[MONI]", "daily": "🎯", "settings": "🟡[WARN]",
    "test": "🟢[INFO]", "backup": "📦[BACKUP]",
    "source_failure": "🔴[ERROR]",
}
_EMOJI = re.compile(r"^[\u2600-\u27bf\U0001f000-\U0001ffff]")
_SOURCE_NAMES = SOURCE_LABELS
_STATUS_NAMES = {
    "ok": "OK", "closed": "休市", "stale": "报价过期",
    "waiting": "等待行情", "error": "异常", "rejected": "行情拒绝",
}


def compact_time(value) -> str:
    """Display the market/trade timestamp as YYYY.MM.DD.HH:MM (Shanghai)."""
    if not value:
        return "未更新"
    try:
        from config import parse_timestamp
        return parse_timestamp(value).strftime("%Y.%m.%d.%H:%M")
    except (ValueError, TypeError, OverflowError):
        return str(value).replace("T", " ")[:16]


def status_prefix(market, *, category="INFO") -> str:
    """Green only for explicitly healthy quote data."""
    # An orderly session close is not a source failure. Preserve the green
    # health indicator when the last accepted quote and its source are known.
    if ((market or {}).get("status") in ("ok", "closed")
            and (market or {}).get("source") and (market or {}).get("price") is not None):
        return f"🟢[{category}]"
    if (market or {}).get("status") in ("waiting", "closed", "stale"):
        return "🟡[WARN]"
    return "🔴[ERROR]"


def label(symbol, *, category="INFO") -> str:
    market = symbol.get("state", {}).get("market", {})
    return f"{status_prefix(market, category=category)}【{symbol['name']}】 ({symbol['symbol']})"


def quote_source(market) -> str:
    source = _SOURCE_NAMES.get(market.get("source"), market.get("source") or "暂无")
    status = _STATUS_NAMES.get(market.get("status"), "未确认")
    return f"📡数据: {source}（{status}）"



def _last_completed_session_close(symbol, at, system):
    """Find the latest expected trading-window end, including overnight sessions.

    Daily snapshot health is based on the *last completed trading window*, not
    the real-time quote-max-age used by strategy signals. Calendar dates set
    in system.closed_dates and weekend rules still apply.
    """
    system = {**SYSTEM_DEFAULTS, **(system or {})}
    cfg = symbol.get("config") or {}
    contract = symbol.get("contract") or {}
    day_end = parse_time(system["day_end"])
    night_start = parse_time(system["night_start"])
    night_end = parse_time(cfg.get("night_end") if cfg.get("night_end") not in (None, "", "off") else system["night_end"])
    candidate_ends = []
    # Plenty for common long weekends; very old stored prices still warn.
    for offset in range(15):
        day = at.date() - timedelta(days=offset)
        candidate_ends.append(datetime.combine(day, day_end, tzinfo=SHANGHAI))
        for window in str(system.get("breaks") or "").split(","):
            if "-" in window:
                candidate_ends.append(datetime.combine(day, parse_time(window.split("-", 1)[0].strip()), tzinfo=SHANGHAI))
        if system.get("night_enabled") and cfg.get("night_end") != "off" and contract.get("exchange") != "CFFEX":
            end_date = day + timedelta(days=1) if night_end <= night_start else day
            candidate_ends.append(datetime.combine(end_date, night_end, tzinfo=SHANGHAI))
    for close in sorted(set(candidate_ends), reverse=True):
        if close > at:
            continue
        # Probe just before closing so breaks / holidays / weekend nights
        # cannot be mistaken for completed trading windows.
        if session_info(system, close - timedelta(seconds=1), cfg, contract)["open"]:
            return close
    return None


def daily_quote_context(symbol, *, at=None, system=None):
    """Classify daily reporting separately from real-time strategy freshness.

    'reference': recent end-of-session quote while the market is closed.
    'missing': missed most recently expected trading window / invalid timestamp.
    'live': during a trading window, or legacy callers without scheduling data.
    This classification is presentation-only; it never authorizes trading.
    """
    if at is None or system is None:
        return "live"
    at = at.astimezone(SHANGHAI)
    cfg = symbol.get("config") or {}
    contract = symbol.get("contract") or {}
    if session_info(system, at, cfg, contract)["open"]:
        return "live"
    close = _last_completed_session_close(symbol, at, system)
    market = (symbol.get("state") or {}).get("market") or {}
    if close is None or market.get("price") is None or not market.get("quote_at"):
        return "missing"
    try:
        quote_at = parse_timestamp(market["quote_at"])
        decimal_value(market["price"], "price")
    except (ValueError, TypeError, ArithmeticError, OverflowError):
        return "missing"
    if quote_at > at + timedelta(minutes=1):
        return "missing"
    # A 15-minute closing window accommodates minor feed pauses. A bounded
    # four-hour tolerance applies ONLY to SHFE/INE evening closings, for
    # contracts whose real night end may be 01:00 or 02:30 despite the default
    # 23:00 setting. Other windows allow five minutes of timestamp drift.
    # Never infer an official exchange settlement price from these quotes.
    late = (timedelta(hours=4) if close.hour >= 21 and contract.get("exchange") in ("SHFE", "INE")
            else timedelta(minutes=5))
    if close - timedelta(minutes=15) <= quote_at <= close + late:
        return "reference"
    return "missing"



def daily_unrealized_line(symbol, *, reference=False, missing_session=False) -> str:
    """Match the status page P&L formula using the same stored quote and multiplier.

    Realized P&L is maintained by confirmed fills and MUST NOT be overwritten
    by mark-to-market P&L. Missing/invalid quotes should not be treated as 0.
    """
    state = symbol.get("state") or {}
    portfolio = state.get("portfolio") or {}
    market = state.get("market") or {}
    cfg = symbol.get("config") or {}
    price = market.get("price")
    try:
        realized = decimal_value(portfolio.get("realized_pnl", "0"), "已实现盈亏")
        long_lots = int(portfolio.get("long_lots", 0))
        short_lots = int(portfolio.get("short_lots", 0))
        if long_lots < 0 or short_lots < 0:
            raise ValueError("持仓手数异常")
        if price is None or str(price).strip() == "":
            if long_lots or short_lots:
                return "💰浮动盈亏: —；总盈亏: —（缺少报价，无法估算）"
            floating = Decimal("0")
        else:
            # The status page uses the stored market.price and cfg.multiplier.
            floating = unrealized(portfolio, price, cfg.get("multiplier"))
        total = realized + floating
    except (ValueError, TypeError, ArithmeticError, OverflowError):
        return "💰浮动盈亏: —；总盈亏: —（报价或持仓数据异常）"

    status = market.get("status")
    note = {
        "stale": "按过期报价估算",
        "closed": "按最近休市报价估算",
        "error": "按最近异常状态报价估算",
        "rejected": "按最近未采用报价估算",
        "waiting": "按最近报价估算",
    }.get(status, "按最近报价估算")
    if reference:
        note = "按上一交易时段报价估算"
    elif missing_session:
        note = "按较早报价估算，请核查"
    if price is None or str(price).strip() == "":
        note = "无持仓"
    return f"💰浮动盈亏: {decimal_text(floating)} 元；总盈亏: {decimal_text(total)} 元（{note}）"


def daily_snapshot_body(symbols, *, at=None, system=None) -> str:
    """Daily report: closed-session reference quotes are not live-feed warnings.

    Keep strategy freshness, persisted market status and stored quote unchanged.
    When called without at/system, preserve the legacy formatting for clients.
    """
    chunks = ["\U0001f3af\u6bcf\u65e5\u5feb\u7167"]
    if not symbols:
        chunks.append("\U0001f7e1[INFO]\u6682\u65e0\u76d1\u6d4b\u5408\u7ea6")
    for symbol in symbols:
        state = symbol.get("state") or {}
        market = state.get("market") or {}
        portfolio = state.get("portfolio") or {}
        context = daily_quote_context(symbol, at=at, system=system)
        if context == "reference":
            heading = f"\U0001f7e2[INFO]\u3010{symbol['name']}\u3011 ({symbol['symbol']})"
            price_label = "\u53c2\u8003\u4ef7"
            source_line = f"\U0001f4e1\u6570\u636e: {_SOURCE_NAMES.get(market.get('source'), market.get('source') or '\u6682\u65e0')}\uff08\u4e0a\u4e2a\u4ea4\u6613\u65f6\u6bb5\u53c2\u8003\u62a5\u4ef7\uff09"
        elif context == "missing":
            heading = f"\U0001f7e1[WARN]\u3010{symbol['name']}\u3011 ({symbol['symbol']})"
            price_label = "\u6700\u8fd1\u4ef7"
            source_line = f"\U0001f4e1\u6570\u636e: {_SOURCE_NAMES.get(market.get('source'), market.get('source') or '\u6682\u65e0')}\uff08\u6700\u8fd1\u4ea4\u6613\u65f6\u6bb5\u62a5\u4ef7\u7f3a\u5931\u6216\u65f6\u95f4\u65e0\u6548\uff09"
        else:
            heading = label(symbol)
            price_label = "\u5f53\u524d"
            source_line = quote_source(market)
        time_label = "\u62a5\u4ef7\u65f6\u95f4" if context != "live" else "\u65f6\u95f4"
        chunks.append("\n" + "\n".join([
            heading,
            f"\U0001f552{time_label}: {compact_time(market.get('quote_at'))}",
            f"\U0001f4b2{price_label}: {market.get('price') if market.get('price') is not None else '\u6682\u65e0'}",
            (f"\u2696\ufe0f\u6301\u4ed3: \u591a {portfolio.get('long_lots', 0)} \u624b / \u7a7a {portfolio.get('short_lots', 0)} \u624b\uff0c"
             f"\u5df2\u5b9e\u73b0\u76c8\u4e8f {portfolio.get('realized_pnl', '0')} \u5143"),
            daily_unrealized_line(symbol, reference=context == "reference", missing_session=context == "missing"),
            source_line,
        ]))
    return "\n".join(chunks)


def manual_trade_body(symbol, side_label, lots, price, portfolio, traded_at) -> str:
    market = symbol.get("state", {}).get("market", {})
    return "\n".join([
        label(symbol, category="TRADE"),
        f"🕒时间: {compact_time(traded_at)}",
        f"🎯成交: {side_label}",
        f"💲价格: {price}",
        f"⚖️手数: {lots} 手；持仓 多 {portfolio.get('long_lots', 0)} 手 / 空 {portfolio.get('short_lots', 0)} 手",
        quote_source(market),
    ])


def signal_trade_body(symbol, signals, market, next_buy=None, next_sell=None) -> str:
    sides = "/".join(dict.fromkeys("买入" if x["side"] == "buy" else "卖出" for x in signals))
    lines = [
        label(symbol, category="TRADE"),
        f"🕒时间: {compact_time(market.get('quote_at'))}",
        f"🎯信号: {sides} {len(signals)} 档",
        f"💲当前: {market.get('price') or '暂无'}",
    ]
    for item in signals:
        direction = "买入" if item["side"] == "buy" else "卖出"
        lines.append(f"📐{direction}线: {item['level']} · {item['lots']} 手")
    lines.extend([
        f"📐下一档: 买入 {next_buy or '—'} / 卖出 {next_sell or '—'}",
        quote_source(market),
        "📝提示信号，不代表已成交",
    ])
    return "\n".join(lines)


def source_failure_body(symbol, quotes, at) -> str:
    """One compact per-contract critical notice, without leaking full URLs."""
    sources = source_options()
    by_source = {q.get("source"): q for q in quotes}
    lines = [
        f"🔴[ERROR]【{symbol['name']}】 ({symbol['symbol']})",
        f"🕒时间: {compact_time(at)}",
        "🚨状态: 3 个行情采集通道全部失效，暂停本轮买卖信号",
        "📡数据: " + " / ".join(label for _, label in sources),
    ]
    for code, label_name in sources:
        item = by_source.get(code, {})
        reason = str(item.get("error") or ("报价时间无效" if item.get("ok") else "未获得有效报价"))
        reason = re.sub(r"[\r\n\t]+", " ", reason).strip()[:90]
        lines.append(f"⚠️{label_name}: {reason}")
    lines.append("🔔消息等级: 4（仅故障首次发生时告警，恢复后重新计次）")
    return "\n".join(lines)


def _icon(line: str) -> str:
    text = line.strip()
    if not text: return ""
    if _EMOJI.match(text): return ""
    if re.search(r"时间|日期|时刻|记录时间|报价时间", text): return "🕒"
    if re.search(r"合约|标的|品种", text): return "📌"
    if re.search(r"行情|源|接口", text): return "📡"
    if re.search(r"买入|开多|平空|下跌", text): return "🟢"
    if re.search(r"卖出|平多|开空|上涨", text): return "🔴"
    if re.search(r"价格|价位|成交价|观察价|当前|监控锚", text): return "💲"
    if re.search(r"手数|手续费|仓位|多仓|空仓|净损益|盈亏|均价", text): return "⚖️"
    if re.search(r"下一|起始线|阶梯|档位|间隔", text): return "📐"
    if re.search(r"已关闭|暂停|异常|失效", text): return "🔴"
    if re.search(r"配置|已保存|已开启|恢复|回滚", text): return "🔄"
    if re.search(r"编号|批次|备注|说明|提醒", text): return "📝"
    return "📋"


def quant_style_message(title: str, body: str, kind: str) -> str:
    # These bodies are already composed line by line. Preserve deliberate blank
    # lines between instruments; do not prepend an additional title or tag.
    raw = str(body).strip()
    if kind == "daily" and raw.startswith("🎯每日快照"):
        return raw
    if kind in ("signal", "manual", "trade", "source_failure") and re.match(r"^[🟢🟡🔴]\[(?:TRADE|INFO|WARN|ERROR)\]【", raw):
        return raw
    lines = [line.strip() for line in str(body).splitlines() if line.strip()]
    if not lines:
        lines = [str(title).strip() or "通知"]
    prefix = PREFIXES.get(str(kind), "🟢[INFO]")
    if kind == "settings" and ("总开关已关闭" in str(title) or any("关闭" in line for line in lines)):
        prefix = "🔴[ERROR]"
    output = [f"{prefix}【{str(title).strip()}】"]
    for line in lines:
        icon = _icon(line)
        output.append(f"{icon}{line}" if icon else line)
    return "\n".join(output)
