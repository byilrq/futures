"""两个独立的国内期货行情适配器（新浪 / 东方财富）。

这里只解析报价，不决定是否交易。``ok`` 表示响应与合约身份及字段有效；
休市报价仍可能 ok=True，调用方必须同时检查 quote_at 的新鲜度和交易时段。
禁止以抓取时间冒充行情时间，禁止将空合约替换为主连。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from contracts import EASTMONEY_MARKETS


SHANGHAI = timezone(timedelta(hours=8))
SOURCE_LABELS = {"sina": "新浪期货", "eastmoney": "东方财富"}
_MAX_BODY = 4 * 1024 * 1024
_USER_AGENT = "Mozilla/5.0 (compatible; FuturesMonitor/1.0)"
_EM_FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f59,f60,f86"


class QuoteError(ValueError):
    """数据源响应不能安全地用于指定合约。"""


def _now():
    return datetime.now(SHANGHAI)


def _iso(value=None):
    value = _now() if value is None else value
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI).isoformat(timespec="seconds")


def _number(value, *, required=False, positive=False):
    try:
        if isinstance(value, bool) or value is None or str(value).strip() in ("", "-", "--", "null"):
            raise ValueError
        result = float(value)
        if not math.isfinite(result) or (positive and result <= 0):
            raise ValueError
        return result
    except (ValueError, TypeError, OverflowError):
        if required:
            raise QuoteError("行情缺少有效价格或数值字段") from None
        return None


def _quote_base(contract, source, received_at=None):
    return {
        "source": source, "label": SOURCE_LABELS[source],
        "symbol": contract.get("symbol", ""), "name": contract.get("name", ""),
        "price": None, "quote_at": None, "received_at": _iso(received_at),
        "ok": False, "error": "",
        "raw_code": (contract.get("sina_symbol", "") if source == "sina"
                     else contract.get("eastmoney_secid", "")),
    }


def _verify_czce_decade(contract, observed_year):
    """东财郑商所 YMM 简码十年后重复；用源数据日期校验完整年份。

    例如 sa1701 与 sa2701 都映射 SA701，不能把 2026 年采样给 sa1701。
    最近十年校验适用于在市商品合约；不猜测十年以上的远期交割月。
    """
    if contract.get("exchange") != "CZCE":
        return
    year = int(contract["year"])
    decade = int(observed_year) // 10 * 10
    candidates = [decade - 10 + year % 10, decade + year % 10, decade + 10 + year % 10]
    inferred = min(candidates, key=lambda y: (abs(y - observed_year), y < observed_year))
    if inferred != year:
        raise QuoteError("郑商所简码存在十年重号，返回日期与请求交割年份不匹配")


def _sina_datetime(day, clock):
    day = str(day).strip()
    clock = str(clock).strip()
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", day):
        raise QuoteError("新浪缺少有效行情日期，不能用抓取时间代替")
    if re.fullmatch(r"\d{6}", clock):
        clock = f"{clock[:2]}:{clock[2:4]}:{clock[4:6]}"
    if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", clock):
        raise QuoteError("新浪行情时间格式错误")
    try:
        return datetime.fromisoformat(day + "T" + clock).replace(tzinfo=SHANGHAI).isoformat()
    except ValueError:
        raise QuoteError("新浪行情日期或时间无效") from None


def parse_sina(payload, contract, *, received_at=None):
    """解析 nf_ 合约响应。商品价格在第 9 项，金融期货价格在第 4 项。"""
    if isinstance(payload, bytes):
        payload = payload.decode("gb18030")
    if not isinstance(payload, str):
        raise QuoteError("新浪响应不是文本")
    wanted = str(contract["sina_symbol"]).upper()
    matches = re.findall(r'(?:var\s+)?hq_str_nf_([A-Za-z0-9]+)\s*=\s*"([^"\r\n]*)"\s*;', payload)
    matches = [body for symbol, body in matches if symbol.upper() == wanted]
    if len(matches) != 1:
        raise QuoteError("新浪未返回所请求的准确合约，或响应中出现重复合约")
    if not matches[0].strip():
        raise QuoteError("新浪合约为空：可能未上市、已到期或该源暂不支持")
    fields = [item.strip() for item in matches[0].split(",")]
    result = _quote_base(contract, "sina", received_at)
    if contract.get("exchange") == "CFFEX":
        if len(fields) < 38:
            raise QuoteError("新浪金融期货字段不完整")
        result.update(
            price=_number(fields[3], required=True, positive=True),
            quote_at=_sina_datetime(fields[36], fields[37]),
            name=fields[49] if len(fields) > 49 and fields[49] else contract["name"],
            open=_number(fields[0], positive=True), high=_number(fields[1], positive=True),
            low=_number(fields[2], positive=True), volume=_number(fields[4]),
            amount=_number(fields[5]), open_interest=_number(fields[6]),
            previous_close=_number(fields[13], positive=True),
            previous_settlement=_number(fields[14], positive=True),
            bid=_number(fields[16], positive=True), ask=_number(fields[26], positive=True),
        )
    else:
        if len(fields) < 18:
            raise QuoteError("新浪商品期货字段不完整")
        result.update(
            price=_number(fields[8], required=True, positive=True),
            quote_at=_sina_datetime(fields[17], fields[1]), name=fields[0] or contract["name"],
            open=_number(fields[2], positive=True), high=_number(fields[3], positive=True),
            low=_number(fields[4], positive=True),
            previous_close=_number(fields[5], positive=True),
            previous_settlement=_number(fields[10], positive=True),
            bid=_number(fields[6], positive=True), ask=_number(fields[7], positive=True),
            open_interest=_number(fields[13]), volume=_number(fields[14]),
        )
    result["ok"] = True
    return result


def parse_eastmoney(payload, contract, *, received_at=None):
    """解析 fltt=1 原始整数报价；按 f59 缩放，绝不假设所有品种两位小数。"""
    if isinstance(payload, (bytes, str)):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise QuoteError("东方财富返回了非 JSON 响应") from None
    if not isinstance(payload, dict) or payload.get("rc", 0) != 0:
        raise QuoteError("东方财富没有返回该合约行情")
    fields = payload.get("data")
    if not isinstance(fields, dict) or not fields:
        raise QuoteError("东方财富合约为空：可能未上市、已到期或该源暂不支持")
    expected_code = contract["eastmoney_secid"].split(".", 1)[1]
    if str(fields.get("f57", "")).lower() != expected_code.lower():
        raise QuoteError("东方财富返回的合约代码与请求不一致，已拒绝使用")
    try:
        precision = fields["f59"]
        if isinstance(precision, bool) or str(precision) not in {str(i) for i in range(9)}:
            raise ValueError
        factor = Decimal(10) ** int(precision)
    except (KeyError, ValueError, TypeError):
        raise QuoteError("东方财富缺少有效小数位 f59，无法安全换算报价") from None

    def price_field(key, required=False):
        raw = fields.get(key)
        try:
            if isinstance(raw, bool) or raw is None:
                raise ValueError
            value = Decimal(str(raw))
            if (not value.is_finite() or value <= 0 or
                    value != value.to_integral_value()):
                raise ValueError
            return float(value / factor)
        except (ValueError, TypeError, InvalidOperation, OverflowError):
            if required:
                raise QuoteError("东方财富原始价格无效或字段格式已变化") from None
            return None

    timestamp = _number(fields.get("f86"), required=True, positive=True)
    if timestamp != int(timestamp) or not 946684800 <= timestamp < 4102444800:
        raise QuoteError("东方财富缺少有效行情时间 f86，不能用抓取时间代替")
    quoted_at = datetime.fromtimestamp(timestamp, SHANGHAI)
    _verify_czce_decade(contract, quoted_at.year)
    result = _quote_base(contract, "eastmoney", received_at)
    result.update(
        price=price_field("f43", required=True),
        quote_at=quoted_at.isoformat(timespec="seconds"),
        name=str(fields.get("f58") or contract["name"]),
        open=price_field("f46"), high=price_field("f44"), low=price_field("f45"),
        previous_close=price_field("f60"), volume=_number(fields.get("f47")),
        amount=_number(fields.get("f48")), ok=True,
    )
    return result


def _http_bytes(url, *, timeout, referer):
    request = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT, "Referer": referer,
        "Accept": "*/*", "Accept-Encoding": "identity", "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(_MAX_BODY + 1)
        if len(body) > _MAX_BODY:
            raise QuoteError("行情响应过大，已停止读取")
        return body


def fetch_sina(contract, timeout=8):
    wanted = urllib.parse.quote(contract["sina_symbol"], safe="")
    url = f"https://hq.sinajs.cn/rn={time.time_ns():x}&list=nf_{wanted}"
    return parse_sina(_http_bytes(url, timeout=timeout,
                                 referer="https://finance.sina.com.cn/"), contract)


def fetch_eastmoney(contract, timeout=8):
    """Primary push2 plus independently hosted push2delay fallback.

    An upstream TLS handshake timeout is network/path availability, not a bad
    contract. Fail over without ever substituting a different symbol or date.
    Both responses are checked by parse_eastmoney. Bound the combined latency
    so one failing upstream cannot consume a full quote cycle.
    """
    query = urllib.parse.urlencode({"secid": contract["eastmoney_secid"],
                                   "fields": _EM_FIELDS, "fltt": "1"})
    started = time.monotonic()
    first_error = None
    for i, host in enumerate(("push2.eastmoney.com", "push2delay.eastmoney.com")):
        # Keep at least a short second attempt in the total budget.
        remaining = max(0.1, float(timeout) - (time.monotonic() - started))
        if i == 0:
            budget = min(remaining, max(1.0, float(timeout) * 0.55))
        else:
            budget = remaining
        try:
            data = _http_bytes(f"https://{host}/api/qt/stock/get?" + query,
                               timeout=budget, referer="https://quote.eastmoney.com/")
            result = parse_eastmoney(data, contract)
            result["endpoint"] = host
            if i:
                result["detail"] = "主接口连接失败，已使用东方财富备用节点"
            return result
        except (urllib.error.URLError, TimeoutError, OSError, QuoteError, ValueError) as exc:
            if first_error is None:
                first_error = str(exc)
            if i:
                raise QuoteError("主接口不可用：" + (first_error or "未知错误")[:100] +
                                 "；备用接口也不可用：" + str(exc)[:120]) from exc
    raise QuoteError("东方财富接口未返回可用报价")


def _safe_fetch(source, contract, timeout):
    started = time.monotonic()
    try:
        fetcher = fetch_sina if source == "sina" else fetch_eastmoney
        result = fetcher(contract, timeout=timeout)
    except Exception as exc:
        result = _quote_base(contract, source)
        if isinstance(exc, urllib.error.HTTPError):
            reason = f"HTTP {exc.code}；数据源暂时不可用"
        elif isinstance(exc, (TimeoutError,)): 
            reason = "行情请求超时"
        elif isinstance(exc, urllib.error.URLError):
            reason = "网络连接失败：" + str(exc.reason)
        else:
            reason = str(exc) or type(exc).__name__
        result["error"] = reason[:300]
    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result


def collect_quotes(contract, timeout=8):
    """并行获取两源；一个源失败不影响另一个；返回顺序恒为新浪、东财。"""
    timeout = float(timeout)
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("行情超时必须在 0–60 秒之间")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="futures-quote") as pool:
        tasks = [pool.submit(_safe_fetch, source, contract, timeout)
                 for source in ("sina", "eastmoney")]
        return [task.result() for task in tasks]


def is_fresh(quote, max_age_seconds=180, *, now=None, future_tolerance_seconds=30):
    """只检查报价新鲜度；交易时段/节假日由运行层控制。"""
    if not quote.get("ok") or _number(quote.get("price"), positive=True) is None:
        return False
    try:
        timestamp = datetime.fromisoformat(str(quote["quote_at"]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return False
        current = _now() if now is None else now
        if isinstance(current, str):
            current = datetime.fromisoformat(current.replace("Z", "+00:00"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=SHANGHAI)
        age = (current - timestamp).total_seconds()
        return -float(future_tolerance_seconds) <= age <= float(max_age_seconds)
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").date()
    return date.fromisoformat(text[:10])


def parse_history(payload, contract, start, end):
    """校验具体合约身份并解析东财日/周/月线的收盘采样。"""
    begin, finish = _as_date(start), _as_date(end)
    if begin > finish:
        raise ValueError("回测开始日期不能晚于结束日期")
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    if not isinstance(payload, dict) or payload.get("rc", 0) != 0:
        raise QuoteError("东方财富历史接口返回错误")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise QuoteError("东方财富未返回该具体月份合约的历史记录，请尝试新浪历史行情")
    market, raw_code = contract["eastmoney_secid"].split(".", 1)
    if (str(data.get("code", "")).lower() != raw_code.lower() or
            str(data.get("market", "")) != market):
        raise QuoteError("历史数据的合约身份与请求不一致，已拒绝使用")
    klines = data.get("klines")
    if not isinstance(klines, list):
        raise QuoteError("历史数据格式错误")
    output = {}
    for line in klines:
        fields = str(line).split(",")
        if len(fields) < 6:
            raise QuoteError("历史 K 线字段不完整")
        day = _as_date(fields[0])
        _verify_czce_decade(contract, day.year)
        if not begin <= day <= finish:
            continue
        price = _number(fields[2], required=True, positive=True)
        row = {"time": day.isoformat(), "price": price,
               "open": _number(fields[1], positive=True),
               "high": _number(fields[3], positive=True),
               "low": _number(fields[4], positive=True),
               "volume": _number(fields[5]),
               "open_interest": _number(fields[12]) if len(fields) > 12 else None,
               "source": "eastmoney"}
        if day in output and output[day] != row:
            raise QuoteError("历史数据包含同日冲突记录")
        output[day] = row
    return [output[key] for key in sorted(output)]


def fetch_history(contract, start, end, period="daily", timeout=12):
    """下载具体月份的未复权收盘采样；不以主连替代过期/缺失合约。

    日线无法还原盘中穿越过程，调用方必须在回测页面说明其采样限制。
    实际可获得日期取决于合约上市时间和数据源保留范围。
    """
    periods = {"daily": "101", "weekly": "102", "monthly": "103"}
    if period not in periods:
        raise ValueError("历史下载支持 daily / weekly / monthly")
    begin, finish = _as_date(start), _as_date(end)
    if begin > finish:
        raise ValueError("回测开始日期不能晚于结束日期")
    params = {
        "secid": contract["eastmoney_secid"], "klt": periods[period], "fqt": "0",
        "lmt": "10000", "end": finish.strftime("%Y%m%d"), "iscca": "1",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64",
        "ut": "7eea3edcaed734bea9cbfc24409ed989", "forcect": "1",
    }
    # ut 为行情网页公开的应用标识，不是用户账户密钥。
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urllib.parse.urlencode(params)
    body = _http_bytes(url, timeout=timeout, referer="https://quote.eastmoney.com/")
    return parse_history(body, contract, begin, finish)
