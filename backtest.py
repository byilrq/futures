"""Sample-price, long-only simulation using the live price-grid state machine.

``parse_csv(bytes, symbol=None)`` returns a list of ``{time, price}`` mappings;
times are ISO 8601 strings in Asia/Shanghai and prices are decimal strings.
The list also has ``price_column`` and ``warnings`` attributes for close-only
CSV input.  Every timestamp must be strictly later than the preceding one.

``run_backtest(points, cfg, options)`` returns stats/trades/signals/series/
warnings/config without changing live state.  Prices are observed samples;
gaps execute at the observed sample plus adverse slippage, never at past grid
lines.  Buy signals open longs and sell signals close available longs.  The
simulation does not model margin, liquidation, short positions or liquidity.

Monetary values are decimal strings.  stats.max_drawdown is an amount;
stats.max_drawdown_pct is a percentage.  Trade realized_pnl is the NET change
caused by that trade, including its fee; stats.realized_pnl is cumulative.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, localcontext
import io
import re
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

if __package__:
    from .strategy import (
        apply_manual,
        decimal_text,
        decimal_value,
        evaluate,
        initial_portfolio,
        initial_state,
        integer_value,
        unrealized,
        validate_config,
    )
else:
    from strategy import (
        apply_manual,
        decimal_text,
        decimal_value,
        evaluate,
        initial_portfolio,
        initial_state,
        integer_value,
        unrealized,
        validate_config,
    )


SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_POINTS = 100_000
MAX_SIGNALS = 100_000
MAX_CSV_BYTES = 20 * 1024 * 1024
_TIME_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?\Z"
)


class PricePoints(list):
    """Ordinary list plus input interpretation metadata for the report."""

    def __init__(self, *args: Any, price_column: str = "price", warnings: Iterable[str] = ()) -> None:
        super().__init__(*args)
        self.price_column = price_column
        self.warnings = list(warnings)


def _time(value: Any, label: str = "时间") -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str) and _TIME_PATTERN.fullmatch(value.strip()):
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{label}无效：{value}") from None
    else:
        raise ValueError(f"{label}须使用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS，可附时区偏移")
    if result.tzinfo is None:
        result = result.replace(tzinfo=SHANGHAI)
    return result.astimezone(SHANGHAI)


def _time_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds" if value.microsecond else "seconds")


def _positive(value: Any, label: str) -> Decimal:
    result = decimal_value(value, label)
    if result <= 0:
        raise ValueError(f"{label}必须大于 0")
    return result


def _nonnegative(value: Any, label: str) -> Decimal:
    result = decimal_value(value, label)
    if result < 0:
        raise ValueError(f"{label}不能小于 0")
    return result


def parse_csv(content: bytes, symbol: str | None = None) -> list[dict[str, str]]:
    """Parse UTF-8 CSV; reject mixed contracts, duplicates and unordered rows.

    Required columns: time and price (close is accepted if price is absent).
    An optional symbol column is compared case-insensitively to ``symbol``;
    different month/continuous contracts are never silently substituted.
    """
    if not isinstance(content, (bytes, bytearray)):
        raise ValueError("CSV 内容必须为 UTF-8 文件字节")
    if len(content) > MAX_CSV_BYTES:
        raise ValueError("CSV 文件不能超过 20 MiB")
    try:
        decoded = bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("CSV 须使用 UTF-8 编码（支持 UTF-8 BOM）") from None
    if "\x00" in decoded:
        raise ValueError("CSV 包含无效的空字符")
    reader = csv.DictReader(io.StringIO(decoded, newline=""))
    try:
        headers = reader.fieldnames
    except csv.Error as error:
        raise ValueError(f"CSV 表头无法读取：{error}") from None
    if not headers:
        raise ValueError("CSV 文件为空，须包含 time,price 表头")
    normalized_headers = [header.strip().lower() for header in headers]
    if any(not header for header in normalized_headers) or len(set(normalized_headers)) != len(normalized_headers):
        raise ValueError("CSV 表头不能为空或重复")
    reader.fieldnames = normalized_headers
    if "time" not in normalized_headers:
        raise ValueError("CSV 缺少 time 列")
    price_column = "price" if "price" in normalized_headers else "close"
    if price_column not in normalized_headers:
        raise ValueError("CSV 缺少 price 列（也可提供 close 收盘价列）")
    warnings = ["CSV 使用 close 列，仅按收盘价回放；未使用 high/low 推测盘中穿越顺序。"] if price_column == "close" else []
    points = PricePoints(price_column=price_column, warnings=warnings)
    expected_symbol = str(symbol).strip().upper() if symbol is not None else None
    if expected_symbol == "":
        raise ValueError("指定合约代码不能为空")
    seen_symbol: str | None = None
    previous_time: datetime | None = None

    try:
        for row in reader:
            line = reader.line_num
            if None in row:
                raise ValueError(f"CSV 第 {line} 行列数超过表头")
            if not any(value and value.strip() for value in row.values()):
                continue
            timestamp = _time(row.get("time"), f"CSV 第 {line} 行时间")
            if previous_time is not None and timestamp <= previous_time:
                label = "时间重复" if timestamp == previous_time else "时间未严格递增"
                raise ValueError(f"CSV 第 {line} 行{label}；请按时间顺序提供每个时间点唯一的一条价格")
            price = _positive(row.get(price_column), f"CSV 第 {line} 行价格")
            if "symbol" in normalized_headers:
                row_symbol = (row.get("symbol") or "").strip().upper()
                if not row_symbol:
                    raise ValueError(f"CSV 第 {line} 行合约代码为空")
                if expected_symbol is not None and row_symbol != expected_symbol:
                    raise ValueError(f"CSV 第 {line} 行合约 {row_symbol} 与所选合约 {expected_symbol} 不一致")
                if seen_symbol is not None and row_symbol != seen_symbol:
                    raise ValueError(f"CSV 第 {line} 行混入其他合约；一次回测只能使用一个固定合约")
                seen_symbol = row_symbol
            points.append({"time": _time_text(timestamp), "price": decimal_text(price)})
            if len(points) > MAX_POINTS:
                raise ValueError(f"CSV 最多支持 {MAX_POINTS} 条价格记录")
            previous_time = timestamp
    except csv.Error as error:
        raise ValueError(f"CSV 格式错误：{error}") from None
    if not points:
        raise ValueError("CSV 没有有效的价格记录")
    return points


def _options(options: Mapping[str, Any] | None, config: Mapping[str, Any]) -> dict[str, Any]:
    if options is None:
        options = {}
    if not isinstance(options, Mapping):
        raise ValueError("回测参数格式不正确")
    normalized = dict(options)
    normalized["initial_cash"] = decimal_text(_positive(options.get("initial_cash", "100000"), "初始资金"))
    normalized["initial_lots"] = integer_value(options.get("initial_lots", 0), "初始多头手数", 0)
    normalized["max_lots"] = integer_value(options.get("max_lots", 100), "最大多头手数", 1)
    if normalized["initial_lots"] > normalized["max_lots"]:
        raise ValueError("初始多头手数不能超过最大多头手数")
    initial_price = _nonnegative(options.get("initial_price", "0"), "初始持仓均价")
    if normalized["initial_lots"] and initial_price <= 0:
        raise ValueError("有初始持仓时必须填写大于 0 的持仓均价")
    normalized["initial_price"] = decimal_text(initial_price)
    normalized["fee_per_lot"] = decimal_text(_nonnegative(options.get("fee_per_lot", "0"), "每手单边手续费"))
    slippage = _nonnegative(options.get("slippage", "0"), "单边滑点")
    with localcontext() as context:
        context.prec = 60
        if slippage % Decimal(config["tick_size"]):
            raise ValueError("单边滑点必须是合约最小变动价位的整数倍")
    normalized["slippage"] = decimal_text(slippage)
    for key in ("start", "end"):
        raw = options.get(key)
        if raw:
            if not isinstance(raw, (str, date, datetime)):
                raise ValueError("回测起止日期格式不正确")
            normalized[key] = _time(raw, "回测起止日期").date().isoformat()
        else:
            normalized[key] = None
    if normalized["start"] and normalized["end"] and normalized["start"] > normalized["end"]:
        raise ValueError("回测开始日期不能晚于结束日期")
    return normalized


def _points(points: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    previous_time: datetime | None = None
    try:
        iterator = iter(points)
    except TypeError:
        raise ValueError("回测价格记录格式不正确") from None
    for index, point in enumerate(iterator, 1):
        if index > MAX_POINTS:
            raise ValueError(f"一次回测最多支持 {MAX_POINTS} 条价格记录")
        if not isinstance(point, Mapping):
            raise ValueError(f"第 {index} 条价格记录格式不正确")
        timestamp = _time(point.get("time"), f"第 {index} 条记录时间")
        if previous_time is not None and timestamp <= previous_time:
            raise ValueError(f"第 {index} 条价格时间重复或未严格递增")
        price = _positive(point.get("price"), f"第 {index} 条记录价格")
        normalized.append({"time": _time_text(timestamp), "date": timestamp.date().isoformat(), "price": price})
        previous_time = timestamp
    return normalized


def run_backtest(
    points: Iterable[Mapping[str, Any]],
    cfg: Mapping[str, Any],
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay accepted samples; this function has no access to the live store.

    Options: initial_cash, initial_lots, initial_price, max_lots, fee_per_lot,
    slippage, optional start/end (inclusive Shanghai calendar dates).  Initial
    cash is an equity accounting base, not a margin constraint.  Existing
    initial lots are valued from initial_price; historical fees are not added.
    """
    config = validate_config(cfg)
    normalized_options = _options(options, config)
    warnings = list(getattr(points, "warnings", []))
    normalized_points = _points(points)
    total_points = len(normalized_points)
    if normalized_options["start"]:
        normalized_points = [point for point in normalized_points if point["date"] >= normalized_options["start"]]
    if normalized_options["end"]:
        normalized_points = [point for point in normalized_points if point["date"] <= normalized_options["end"]]
    if len(normalized_points) < 2:
        source = normalized_options.get("source", "recorded")
        advice = {
            "recorded": "可尝试扩大日期范围，或使用东方财富日线、上传 CSV。",
            "eastmoney_daily": "请检查日期范围及具体月份合约是否有历史行情，或改用上传 CSV。",
            "csv": "请检查 CSV 时间列和日期范围，至少提供两个不同时间点。",
        }.get(source, "请检查日期范围。")
        raise ValueError(
            f"所选数据只有 {len(normalized_points)} 条有效记录（日期筛选前 {total_points} "
            f"条），回测至少需要 2 条时间递增的价格记录。{advice}"
        )

    warnings.extend([
        "仅按输入的采样价/收盘价回放；首次价格只建立基线，不补发此前已经越过的档位。",
        "模拟只做多：买入信号开多，卖出信号仅平已有多头；未模拟做空、保证金、强平、成交量和涨跌停排队。",
        "每档在触发时观察到的价格加不利滑点成交；跳空不会按过去的档位价格虚拟成交。手续费为每手单边费用。",
        "初始资金仅作为权益计算基准，不代表可用保证金；手数由最大持仓限制，可能出现负权益。",
    ])
    if normalized_options["initial_lots"]:
        warnings.append("初始持仓按填写均价计入盈亏，历史开仓手续费不追溯；首个采样点的权益可能不同于初始资金。")
    if normalized_options.get("source") in ("sina_daily", "eastmoney_daily") and not any("close 列" in warning for warning in warnings):
        warnings.append("行情来源为日线，仅使用每日收盘价，不能还原日内档位触发顺序。")
    strategy = initial_state(config)
    strategy["cycle_id"] = "backtest"
    portfolio = initial_portfolio()
    if normalized_options["initial_lots"]:
        portfolio["long_lots"] = normalized_options["initial_lots"]
        portfolio["long_avg"] = normalized_options["initial_price"]

    cash = Decimal(normalized_options["initial_cash"])
    multiplier = Decimal(config["multiplier"])
    fee_per_lot = Decimal(normalized_options["fee_per_lot"])
    slippage = Decimal(normalized_options["slippage"])
    maximum_lots = normalized_options["max_lots"]
    peak_equity = cash
    maximum_drawdown = Decimal(0)
    maximum_drawdown_pct = Decimal(0)
    signals: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    skipped = 0
    skipped_lots = 0

    with localcontext() as context:
        context.prec = 34
        for point in normalized_points:
            price = point["price"]
            strategy, new_signals, _ = evaluate(config, strategy, price)
            if len(signals) + len(new_signals) > MAX_SIGNALS:
                raise ValueError(f"回测信号超过 {MAX_SIGNALS} 条，请缩短区间或增大点数间隔")
            for signal in new_signals:
                side = signal["side"]
                requested_lots = signal["lots"]
                available = maximum_lots - portfolio["long_lots"] if side == "buy" else portfolio["long_lots"]
                execution_lots = min(requested_lots, max(0, available))
                missing_lots = requested_lots - execution_lots
                recorded_signal = dict(signal, time=point["time"], executed_lots=execution_lots, skipped_lots=missing_lots)
                if missing_lots:
                    skipped += 1
                    skipped_lots += missing_lots
                    recorded_signal["reason"] = "达到最大多头手数" if side == "buy" else "多头持仓不足，不开空仓"
                else:
                    recorded_signal["reason"] = "已模拟成交"
                signals.append(recorded_signal)
                if not execution_lots:
                    continue
                execution_price = price + slippage if side == "buy" else price - slippage
                if execution_price <= 0:
                    raise ValueError("单边滑点使模拟卖出价格不大于 0，请减小滑点")
                fee = fee_per_lot * execution_lots
                realized_before = Decimal(portfolio["realized_pnl"])
                action = "open_long" if side == "buy" else "close_long"
                portfolio = apply_manual(portfolio, action, execution_price, execution_lots, fee, multiplier)
                equity = cash + Decimal(portfolio["realized_pnl"]) + unrealized(portfolio, price, multiplier)
                trades.append({
                    "time": point["time"],
                    "side": side,
                    "action": action,
                    "price": decimal_text(execution_price),
                    "lots": execution_lots,
                    "fee": decimal_text(fee),
                    "realized_pnl": decimal_text(Decimal(portfolio["realized_pnl"]) - realized_before),
                    "equity": decimal_text(equity),
                    "reason": ("跌破买入线 " if side == "buy" else "突破卖出线 ") + signal["level"],
                    "level": signal["level"],
                    "end_lots": portfolio["long_lots"],
                })
            equity = cash + Decimal(portfolio["realized_pnl"]) + unrealized(portfolio, price, multiplier)
            peak_equity = max(peak_equity, equity)
            drawdown = peak_equity - equity
            maximum_drawdown = max(maximum_drawdown, drawdown)
            if peak_equity > 0:
                maximum_drawdown_pct = max(maximum_drawdown_pct, drawdown / peak_equity * 100)
            series.append({"time": point["time"], "price": decimal_text(price), "equity": decimal_text(equity)})

        final_unrealized = unrealized(portfolio, normalized_points[-1]["price"], multiplier)
        total_pnl = Decimal(portfolio["realized_pnl"]) + final_unrealized
        stats = {
            "points": len(normalized_points),
            "signals": len(signals),
            "trades": len(trades),
            "skipped": skipped,
            "skipped_lots": skipped_lots,
            "end_lots": portfolio["long_lots"],
            "realized_pnl": portfolio["realized_pnl"],
            "unrealized_pnl": decimal_text(final_unrealized),
            "fees": portfolio["fees"],
            "total_pnl": decimal_text(total_pnl),
            "max_drawdown": decimal_text(maximum_drawdown),
            "max_drawdown_pct": decimal_text(maximum_drawdown_pct),
            "ending_equity": decimal_text(cash + total_pnl),
            "return_pct": decimal_text(total_pnl / cash * 100),
            "initial_cash": decimal_text(cash),
            "initial_lots": normalized_options["initial_lots"],
            "start": normalized_points[0]["time"],
            "end": normalized_points[-1]["time"],
        }
    return {
        "stats": stats,
        "trades": trades,
        "signals": signals,
        "series": series,
        "warnings": list(dict.fromkeys(warnings)),
        "config": {
            "strategy": config,
            "options": normalized_options,
            "execution": "sampled_price",
            "position_mode": "long_only",
        },
    }
