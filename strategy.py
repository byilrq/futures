"""Pure price-grid strategy and manually recorded futures position accounting.

``evaluate(config, state, price, baseline=False)`` never performs I/O and never
mutates its inputs.  It returns ``(new_state, signals, reason)``.  The caller is
responsible for rejecting stale/out-of-order quotes, persisting the returned
state and events atomically, and updating ``last_quote_at``/``last_source``.

Prices cross a line strictly: equal prices do not trigger.  A return-mode
ladder rearms its buy side after an observed return to the lower line and its
sell side after a return to the upper line.  Manual mode only rearms when the
caller explicitly replaces the state with ``initial_state(config)``.

A rebaseline (initial quote, resume, provider change or rollback) never emits
signals.  It preserves consumed levels and skips any levels already passed in
the unobserved interval.  Notifications do not modify recorded positions.

All persisted decimal values are plain strings; lot counts are integers.
``realized_pnl`` is net of *all* recorded fees, including opening fees.
``unrealized`` returns a Decimal, without subtracting those fees a second time.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
import re
from typing import Any, Mapping
from uuid import uuid4


MAX_LEVELS_PER_QUOTE = 200
_MAX_DECIMAL = Decimal("1e18")
_MIN_NONZERO_DECIMAL = Decimal("1e-18")
_INTEGER_PATTERN = re.compile(r"\+?\d+\Z")


def decimal_value(value: Any, label: str = "数值") -> Decimal:
    """Read an ordinary finite decimal without converting through a float.

    Bounds protect against pathological exponent strings, not market movement.
    Computed weighted average costs may have more decimal places than a tick.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label}必须是有效数字")
    raw = str(value).strip()
    if not raw or len(raw) > 200:
        raise ValueError(f"{label}必须是有效数字")
    try:
        result = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label}必须是有效数字") from None
    if not result.is_finite():
        raise ValueError(f"{label}不能是 NaN 或无穷大")
    if abs(result) > _MAX_DECIMAL or (result and abs(result) < _MIN_NONZERO_DECIMAL):
        raise ValueError(f"{label}超出支持的数值范围")
    return result


def decimal_text(value: Decimal | str | int) -> str:
    """Serialize a decimal without scientific notation or context rounding."""
    number = value if isinstance(value, Decimal) else decimal_value(value)
    if not number:
        return "0"
    rendered = format(number, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def integer_value(value: Any, label: str, minimum: int = 0, maximum: int = 1_000_000) -> int:
    """Require an integer representation; reject 1.0, booleans and fractions."""
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是整数")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and len(value.strip()) <= 20 and _INTEGER_PATTERN.fullmatch(value.strip()):
        result = int(value.strip())
    else:
        raise ValueError(f"{label}必须是整数")
    if result < minimum or result > maximum:
        raise ValueError(f"{label}必须在 {minimum} 至 {maximum} 之间")
    return result


def _boolean(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off", ""):
            return False
    raise ValueError(f"{label}必须为开启或关闭")


def _positive(value: Any, label: str) -> Decimal:
    number = decimal_value(value, label)
    if number <= 0:
        raise ValueError(f"{label}必须大于 0")
    return number


def _nonnegative(value: Any, label: str) -> Decimal:
    number = decimal_value(value, label)
    if number < 0:
        raise ValueError(f"{label}不能小于 0")
    return number


def _aligned(value: Decimal, tick: Decimal, label: str) -> None:
    with localcontext() as context:
        context.prec = 60
        if value % tick:
            raise ValueError(f"{label}必须是最小变动价位 {decimal_text(tick)} 的整数倍")


def validate_config(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a configured strategy and return a normalized independent dict.

    Required: lower_price, upper_price, down_step, up_step.  Defaults:
    signal_lots=1, multiplier=1, tick_size=1, rearm_mode='return'.  Existing
    unrelated config keys are retained.  Empty disabled strategies are handled
    by the application wrapper; this function always requires valid lines.
    """
    if not isinstance(cfg, Mapping):
        raise ValueError("策略配置格式不正确")
    normalized = deepcopy(dict(cfg))
    labels = {
        "lower_price": "买入起始价",
        "upper_price": "卖出起始价",
        "down_step": "下跌间隔",
        "up_step": "上涨间隔",
    }
    numbers = {key: _positive(cfg.get(key), label) for key, label in labels.items()}
    if numbers["lower_price"] >= numbers["upper_price"]:
        raise ValueError("买入起始价必须低于卖出起始价")
    tick = _positive(cfg.get("tick_size", "1"), "最小变动价位")
    multiplier = _positive(cfg.get("multiplier", "1"), "合约乘数")
    for key, number in numbers.items():
        _aligned(number, tick, labels[key])
        normalized[key] = decimal_text(number)
    normalized["tick_size"] = decimal_text(tick)
    normalized["multiplier"] = decimal_text(multiplier)
    normalized["signal_lots"] = integer_value(cfg.get("signal_lots", 1), "信号手数", 1)
    mode = str(cfg.get("rearm_mode", "return")).strip().lower()
    if mode not in ("return", "manual"):
        raise ValueError("重新武装方式必须为 return 或 manual")
    normalized["rearm_mode"] = mode
    if "enabled" in cfg:
        normalized["enabled"] = _boolean(cfg["enabled"], "策略开关")
    return normalized


def initial_state(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Create a new explicit strategy cycle; no price is assumed observed."""
    config = validate_config(cfg)
    return {
        "last_price": None,
        "next_buy": config["lower_price"],
        "next_sell": config["upper_price"],
        "buy_count": 0,
        "sell_count": 0,
        "cycle_id": uuid4().hex,
        "needs_baseline": True,
        "last_quote_at": None,
        "last_source": None,
        "buy_cycle": 0,
        "sell_cycle": 0,
        "total_buy_count": 0,
        "total_sell_count": 0,
    }


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _normalized_state(state: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(state, Mapping):
        raise ValueError("策略状态格式不正确")
    result = deepcopy(dict(state))
    result.setdefault("last_price", None)
    result.setdefault("next_buy", config["lower_price"])
    result.setdefault("next_sell", config["upper_price"])
    if result["next_buy"] is None:
        result["next_buy"] = config["lower_price"]
    if result["next_sell"] is None:
        result["next_sell"] = config["upper_price"]
    result.setdefault("cycle_id", uuid4().hex)
    result.setdefault("needs_baseline", result["last_price"] is None)
    result.setdefault("last_quote_at", None)
    result.setdefault("last_source", None)
    for name in ("buy_count", "sell_count", "buy_cycle", "sell_cycle", "total_buy_count", "total_sell_count"):
        result[name] = integer_value(result.get(name, 0), "策略累计计数", 0, 1_000_000_000)
    if not isinstance(result["cycle_id"], str) or not result["cycle_id"] or len(result["cycle_id"]) > 100:
        raise ValueError("策略周期标识不正确")
    result["needs_baseline"] = _boolean(result["needs_baseline"], "价格基线状态")
    # Do not silently reset incompatible persisted lines: configuration changes
    # must explicitly reset the strategy in the application transaction.
    next_buy = decimal_value(result["next_buy"], "下一买入线")
    next_sell = decimal_value(result["next_sell"], "下一卖出线")
    lower = Decimal(config["lower_price"])
    upper = Decimal(config["upper_price"])
    with localcontext() as context:
        context.prec = 60
        if next_buy > lower or (lower - next_buy) % Decimal(config["down_step"]):
            raise ValueError("买入档位与当前配置不一致，请重置策略周期")
        if next_sell < upper or (next_sell - upper) % Decimal(config["up_step"]):
            raise ValueError("卖出档位与当前配置不一致，请重置策略周期")
    result["next_buy"] = decimal_text(next_buy)
    result["next_sell"] = decimal_text(next_sell)
    if result["last_price"] is not None:
        result["last_price"] = decimal_text(_positive(result["last_price"], "上次价格"))
    return result


def evaluate(
    cfg: Mapping[str, Any],
    state: Mapping[str, Any],
    price: Any,
    baseline: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Apply one accepted price sample without modifying recorded holdings.

    Signals contain side, level, price, lots and cycle_id.  The signal price is
    the observed price, not a fictitious fill at the crossed line.  A gap that
    crosses more than 200 new levels raises ValueError with the input unchanged.
    Source/timestamp acceptance is owned by the caller, not this pure function.
    """
    config = validate_config(cfg)
    result = _normalized_state(state, config)
    current = _positive(price, "行情价格")
    lower = Decimal(config["lower_price"])
    upper = Decimal(config["upper_price"])
    down = Decimal(config["down_step"])
    up = Decimal(config["up_step"])
    next_buy = Decimal(result["next_buy"])
    next_sell = Decimal(result["next_sell"])
    needs_baseline = bool(baseline or result["needs_baseline"] or result["last_price"] is None)

    with localcontext() as context:
        context.prec = 60
        if needs_baseline:
            # With strict crossings, an exactly-equal line remains eligible for
            # the next sample.  Historical levels above/below the sample do not.
            candidate_buy = lower - max(0, _ceil((lower - current) / down)) * down
            candidate_sell = upper + max(0, _ceil((current - upper) / up)) * up
            skipped = candidate_buy < next_buy or candidate_sell > next_sell
            result["next_buy"] = decimal_text(min(next_buy, candidate_buy))
            result["next_sell"] = decimal_text(max(next_sell, candidate_sell))
            result["last_price"] = decimal_text(current)
            result["needs_baseline"] = False
            reason = "已建立价格基线；跳过未观察到的已越过档位，等待下一次击穿" if skipped else "已建立价格基线，等待下一次击穿"
            return result, [], reason

        previous = Decimal(result["last_price"])
        rearmed: list[str] = []
        if config["rearm_mode"] == "return":
            if previous < lower <= current:
                next_buy = lower
                result["buy_count"] = 0
                result["buy_cycle"] += 1
                rearmed.append("买入")
            if previous > upper >= current:
                next_sell = upper
                result["sell_count"] = 0
                result["sell_cycle"] += 1
                rearmed.append("卖出")

        signals: list[dict[str, Any]] = []
        side = ""
        count = 0
        if current < previous and previous >= next_buy > current:
            side = "buy"
            count = _ceil((next_buy - current) / down)
        elif current > previous and previous <= next_sell < current:
            side = "sell"
            count = _ceil((current - next_sell) / up)

        if count > MAX_LEVELS_PER_QUOTE:
            raise ValueError(f"单次行情跨越 {count} 档，超过 {MAX_LEVELS_PER_QUOTE} 档上限；本次未推进策略，请检查价格和间隔")
        if side:
            line = next_buy if side == "buy" else next_sell
            step = -down if side == "buy" else up
            for index in range(count):
                signals.append({
                    "side": side,
                    "level": decimal_text(line + index * step),
                    "price": decimal_text(current),
                    "lots": config["signal_lots"],
                    "cycle_id": f"{result['cycle_id']}:{side}:{result[side + '_cycle']}",
                })
            result[side + "_count"] += count
            result["total_" + side + "_count"] += count
            if side == "buy":
                next_buy -= down * count
            else:
                next_sell += up * count

        result["next_buy"] = decimal_text(next_buy)
        result["next_sell"] = decimal_text(next_sell)
        result["last_price"] = decimal_text(current)
        result["needs_baseline"] = False

    if signals:
        direction = "向下" if side == "buy" else "向上"
        action = "买入" if side == "buy" else "卖出"
        reason = f"{direction}击穿 {count} 档{action}线；仅记录信号，不变更持仓"
    elif rearmed:
        reason = "价格回到起始线，" + "、".join(rearmed) + "侧已重新武装"
    elif current == previous:
        reason = "价格未变，无新增信号"
    else:
        reason = "尚未击穿下一档价格线"
    return result, signals, reason


def initial_portfolio() -> dict[str, Any]:
    return {
        "long_lots": 0,
        "long_avg": "0",
        "short_lots": 0,
        "short_avg": "0",
        "realized_pnl": "0",
        "fees": "0",
    }


def _portfolio(portfolio: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(portfolio, Mapping):
        raise ValueError("持仓记录格式不正确")
    result = deepcopy(dict(portfolio))
    for side, label in (("long", "多头"), ("short", "空头")):
        lots = integer_value(result.get(side + "_lots", 0), label + "手数", 0, 1_000_000_000)
        average = _nonnegative(result.get(side + "_avg", "0"), label + "均价")
        if lots and average <= 0:
            raise ValueError(label + "持仓均价必须大于 0")
        result[side + "_lots"] = lots
        result[side + "_avg"] = decimal_text(average if lots else Decimal(0))
    result["realized_pnl"] = decimal_text(decimal_value(result.get("realized_pnl", "0"), "已实现盈亏"))
    result["fees"] = decimal_text(_nonnegative(result.get("fees", "0"), "累计手续费"))
    return result


def apply_manual(
    portfolio: Mapping[str, Any],
    side: str,
    price: Any,
    lots: Any,
    fee: Any,
    multiplier: Any,
) -> dict[str, Any]:
    """Record one confirmed manual fill and return an independent portfolio.

    side is one of open_long, close_long, open_short, close_short.  fee is the
    TOTAL fee for this fill (not a per-lot value).  Closing excess lots is an
    error; no implicit reversal or short sale is performed.
    """
    if side not in ("open_long", "close_long", "open_short", "close_short"):
        raise ValueError("交易方向必须为买入开多、卖出平多、卖出开空或买入平空")
    result = _portfolio(portfolio)
    fill_price = _positive(price, "成交价格")
    fill_lots = integer_value(lots, "成交手数", 1)
    fill_fee = _nonnegative(fee, "本次手续费")
    contract_multiplier = _positive(multiplier, "合约乘数")
    action, direction = side.split("_")
    held = result[direction + "_lots"]
    average = Decimal(result[direction + "_avg"])
    realized = Decimal(result["realized_pnl"])

    with localcontext() as context:
        context.prec = 34
        if action == "open":
            updated_lots = held + fill_lots
            if updated_lots > 1_000_000_000:
                raise ValueError("累计持仓手数超出支持范围")
            average = (average * held + fill_price * fill_lots) / updated_lots
        else:
            if fill_lots > held:
                label = "多头" if direction == "long" else "空头"
                raise ValueError(f"平仓手数超过现有{label}持仓（{held} 手）")
            updated_lots = held - fill_lots
            spread = fill_price - average if direction == "long" else average - fill_price
            realized += spread * contract_multiplier * fill_lots
            if not updated_lots:
                average = Decimal(0)
        result[direction + "_lots"] = updated_lots
        result[direction + "_avg"] = decimal_text(average)
        result["fees"] = decimal_text(Decimal(result["fees"]) + fill_fee)
        result["realized_pnl"] = decimal_text(realized - fill_fee)
    return result


def unrealized(portfolio: Mapping[str, Any], price: Any, multiplier: Any) -> Decimal:
    """Return combined long/short unrealized P&L; opening fees are already realized."""
    positions = _portfolio(portfolio)
    mark = _positive(price, "估值价格")
    contract_multiplier = _positive(multiplier, "合约乘数")
    with localcontext() as context:
        context.prec = 34
        return (
            (mark - Decimal(positions["long_avg"])) * positions["long_lots"]
            + (Decimal(positions["short_avg"]) - mark) * positions["short_lots"]
        ) * contract_multiplier
