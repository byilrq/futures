"""Compact Quant-like notification bodies; transport titles stay optional.

Daily snapshots deliberately have NO ntfy/Gotify title. The only caption is
"🎯每日快照" inside the body. Quote timestamp is shown on its own line, never
appended to that caption. Actual fills and strategy SIGNALS are distinct.
"""
from __future__ import annotations

import re
from decimal import Decimal

from strategy import decimal_text, decimal_value, unrealized

PREFIXES = {
    "signal": "🎯[TRADE]", "manual": "🎯[TRADE]", "trade": "🎯[TRADE]",
    "monitor": "🏹[MONI]", "daily": "🎯", "settings": "🟡[WARN]",
    "test": "🟢[INFO]", "backup": "📦[BACKUP]",
}
_EMOJI = re.compile(r"^[\u2600-\u27bf\U0001f000-\U0001ffff]")
_SOURCE_NAMES = {"sina": "新浪期货", "eastmoney": "东方财富"}
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



def daily_unrealized_line(symbol) -> str:
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
    if price is None or str(price).strip() == "":
        note = "无持仓"
    return f"💰浮动盈亏: {decimal_text(floating)} 元；总盈亏: {decimal_text(total)} 元（{note}）"


def daily_snapshot_body(symbols) -> str:
    """No execution time in header and no event IDs or last_reason clutter."""
    chunks = ["🎯每日快照"]
    if not symbols:
        chunks.append("🟡[INFO]暂无监测合约")
    for symbol in symbols:
        state = symbol.get("state", {})
        market = state.get("market", {})
        portfolio = state.get("portfolio", {})
        chunks.append("\n" + "\n".join([
            label(symbol),
            f"🕒时间: {compact_time(market.get('quote_at'))}",
            f"💲当前: {market.get('price') or '暂无'}",
            (f"⚖️持仓: 多 {portfolio.get('long_lots', 0)} 手 / 空 {portfolio.get('short_lots', 0)} 手，"
             f"已实现盈亏 {portfolio.get('realized_pnl', '0')} 元"),
            daily_unrealized_line(symbol),
            quote_source(market),
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
    if kind in ("signal", "manual", "trade") and re.match(r"^[🟢🟡🔴]\[(?:TRADE|INFO|WARN|ERROR)\]【", raw):
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
