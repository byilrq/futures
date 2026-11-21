"""国内期货合约代码规范化；不把连续合约替换成某个交割月。

``multiplier`` 是每手价格变动 1 点所对应的人民币金额，而不一定是
交易单位中的实物数量（例如鸡蛋以元/500 千克报价，国债以百元净价报价）。
元数据为内置默认值；行情是否存在仍须向数据源查询。
"""
from __future__ import annotations

from datetime import date, datetime
import re


EXCHANGES = {
    "SHFE": "上海期货交易所",
    "DCE": "大连商品交易所",
    "CZCE": "郑州商品交易所",
    "INE": "上海国际能源交易中心",
    "CFFEX": "中国金融期货交易所",
    "GFEX": "广州期货交易所",
}

# 2026-11-08 由东方财富 gnweb 市场表实测核对。中金所为 220，而非 8。
EASTMONEY_MARKETS = {"SHFE": 113, "DCE": 114, "CZCE": 115,
                     "INE": 142, "CFFEX": 220, "GFEX": 225}

_ALIASES = {
    "SHF": "SHFE", "上期所": "SHFE", "上海期货交易所": "SHFE",
    "大商所": "DCE", "大连商品交易所": "DCE",
    "ZCE": "CZCE", "CZC": "CZCE", "郑商所": "CZCE", "郑州商品交易所": "CZCE",
    "上期能源": "INE", "上海国际能源交易中心": "INE",
    "CFX": "CFFEX", "中金所": "CFFEX", "中国金融期货交易所": "CFFEX",
    "GFE": "GFEX", "广期所": "GFEX", "广州期货交易所": "GFEX",
}

# product -> (中文品种名, 交易所, 元/点/手, 最小价格变动)
# 保证金率、手续费、最后交易日随合约调整，刻意不作为静态默认值写在这里。
_PRODUCT_ROWS = {
    "a": ("豆一", "DCE", 10, 1), "b": ("豆二", "DCE", 10, 1),
    "m": ("豆粕", "DCE", 10, 1), "y": ("豆油", "DCE", 10, 2),
    "p": ("棕榈油", "DCE", 10, 2), "c": ("玉米", "DCE", 10, 1),
    "cs": ("玉米淀粉", "DCE", 10, 1), "l": ("塑料", "DCE", 5, 1),
    "v": ("PVC", "DCE", 5, 1), "pp": ("聚丙烯", "DCE", 5, 1),
    "eg": ("乙二醇", "DCE", 10, 1), "eb": ("苯乙烯", "DCE", 5, 1),
    "j": ("焦炭", "DCE", 100, 0.5), "jm": ("焦煤", "DCE", 60, 0.5),
    "i": ("铁矿石", "DCE", 100, 0.5), "jd": ("鸡蛋", "DCE", 10, 1),
    "lh": ("生猪", "DCE", 16, 5), "pg": ("液化石油气", "DCE", 20, 1),
    "rr": ("粳米", "DCE", 10, 1), "fb": ("纤维板", "DCE", 10, 0.5),
    "bb": ("胶合板", "DCE", 500, 0.05), "lg": ("原木", "DCE", 90, 0.5),
    "cu": ("铜", "SHFE", 5, 10), "al": ("铝", "SHFE", 5, 5),
    "zn": ("锌", "SHFE", 5, 5), "pb": ("铅", "SHFE", 5, 5),
    "ni": ("镍", "SHFE", 1, 10), "sn": ("锡", "SHFE", 1, 10),
    "au": ("黄金", "SHFE", 1000, 0.02), "ag": ("白银", "SHFE", 15, 1),
    "rb": ("螺纹钢", "SHFE", 10, 1), "hc": ("热轧卷板", "SHFE", 10, 1),
    "ss": ("不锈钢", "SHFE", 5, 5), "fu": ("燃料油", "SHFE", 10, 1),
    "bu": ("沥青", "SHFE", 10, 1), "ru": ("天然橡胶", "SHFE", 10, 5),
    "sp": ("纸浆", "SHFE", 10, 2), "ao": ("氧化铝", "SHFE", 20, 1),
    "br": ("丁二烯橡胶", "SHFE", 5, 5), "wr": ("线材", "SHFE", 10, 1),
    "cf": ("棉花", "CZCE", 5, 5), "sr": ("白糖", "CZCE", 10, 1),
    "ta": ("PTA", "CZCE", 5, 2), "ma": ("甲醇", "CZCE", 10, 1),
    "oi": ("菜籽油", "CZCE", 10, 1), "rm": ("菜粕", "CZCE", 10, 1),
    "fg": ("玻璃", "CZCE", 20, 1), "sa": ("纯碱", "CZCE", 20, 1),
    "zc": ("动力煤", "CZCE", 100, 0.2), "pf": ("短纤", "CZCE", 5, 2),
    "cy": ("棉纱", "CZCE", 5, 5), "ap": ("苹果", "CZCE", 10, 1),
    "cj": ("红枣", "CZCE", 5, 5), "ur": ("尿素", "CZCE", 20, 1),
    "sf": ("硅铁", "CZCE", 5, 2), "sm": ("锰硅", "CZCE", 5, 2),
    "pk": ("花生", "CZCE", 5, 2), "px": ("对二甲苯", "CZCE", 5, 2),
    "sh": ("烧碱", "CZCE", 30, 1), "wh": ("强麦", "CZCE", 20, 1),
    "pm": ("普麦", "CZCE", 50, 1), "ri": ("早籼稻", "CZCE", 20, 1),
    "lr": ("晚籼稻", "CZCE", 20, 1), "jr": ("粳稻", "CZCE", 20, 1),
    "rs": ("油菜籽", "CZCE", 10, 1), "pr": ("瓶片", "CZCE", 15, 2),
    "sc": ("原油", "INE", 1000, 0.1), "nr": ("20号胶", "INE", 10, 5),
    "lu": ("低硫燃料油", "INE", 10, 1), "bc": ("国际铜", "INE", 5, 10),
    "ec": ("集运指数欧线", "INE", 50, 0.1),
    "si": ("工业硅", "GFEX", 5, 5), "lc": ("碳酸锂", "GFEX", 1, 20),
    "ps": ("多晶硅", "GFEX", 3, 5),
    "if": ("沪深300股指", "CFFEX", 300, 0.2),
    "ih": ("上证50股指", "CFFEX", 300, 0.2),
    "ic": ("中证500股指", "CFFEX", 200, 0.2),
    "im": ("中证1000股指", "CFFEX", 200, 0.2),
    "t": ("10年期国债", "CFFEX", 10000, 0.005),
    "tf": ("5年期国债", "CFFEX", 10000, 0.005),
    "ts": ("2年期国债", "CFFEX", 20000, 0.002),
    "tl": ("30年期国债", "CFFEX", 10000, 0.01),
}


def _exchange_name(exchange):
    if exchange is None or str(exchange).strip().upper() in ("", "AUTO", "自动"):
        return None
    value = str(exchange).strip().upper()
    value = _ALIASES.get(value, value)
    if value not in EXCHANGES:
        raise ValueError("不支持的交易所；可选 SHFE / DCE / CZCE / INE / CFFEX / GFEX")
    return value


def supported_products():
    """返回内置品种表的独立副本，供说明页面使用。"""
    return [dict(product=code, name=row[0], exchange=row[1], multiplier=row[2],
                 tick_size=row[3]) for code, row in sorted(_PRODUCT_ROWS.items())]


def normalize_contract(code, exchange=None, *, as_of=None):
    """规范化具体月份合约；m2709 -> m2709，SA709 -> sa2709。

    郑商所三位日期缺少十位年份，按距离 as_of 最近的十年推断；页面和
    数据库始终存四位日期，避免重新启动时重新推断同一个已保存合约。
    支持 ``DCE.m2709`` / ``m2709.DCE``，显式交易所冲突会报错。
    """
    if not isinstance(code, str) or len(code) > 32:
        raise ValueError("合约代码应为英文品种代码加年月，例如 m2709、rb2710、SA2701")
    code = code.strip()
    chosen = _exchange_name(exchange)
    if "." in code:
        parts = code.split(".")
        if len(parts) != 2:
            raise ValueError("合约代码格式错误")
        if re.fullmatch(r"[A-Za-z]{1,3}\d{3,4}", parts[0]):
            code, suffix = parts
        else:
            suffix, code = parts
        suffix = _exchange_name(suffix)
        if chosen and chosen != suffix:
            raise ValueError("合约代码中的交易所与所选交易所不一致")
        chosen = suffix
    match = re.fullmatch(r"([A-Za-z]{1,3})(\d{3,4})", code)
    if not match:
        raise ValueError("请填写具体交割月合约，如 m2709；不支持 M0、888、9999 等主连/指数代码")
    product, digits = match.group(1).lower(), match.group(2)
    if product not in _PRODUCT_ROWS:
        raise ValueError(f"暂未内置品种 {product}，不能猜测其交易所和合约乘数")
    product_name, market, multiplier, tick_size = _PRODUCT_ROWS[product]
    if chosen and chosen != market:
        raise ValueError(f"{product} 属于 {EXCHANGES[market]}（{market}），与所选交易所不一致")
    month = int(digits[-2:])
    if not 1 <= month <= 12:
        raise ValueError("合约月份必须为 01–12；主连和指数代码不能用于本策略")
    note = ""
    if len(digits) == 3:
        if market != "CZCE":
            raise ValueError("只有郑商所接受三位年月简码；其他交易所请用四位年月，例如 m2709")
        today = as_of or date.today()
        if isinstance(today, str):
            today = date.fromisoformat(today[:10])
        if isinstance(today, datetime):
            today = today.date()
        decade = today.year // 10 * 10
        candidates = [decade - 10 + int(digits[0]), decade + int(digits[0]),
                      decade + 10 + int(digits[0])]
        year = min(candidates, key=lambda y: (abs(y - today.year), y < today.year))
        if year < 2000 or year > 2099:
            raise ValueError("无法确定三位简码的完整年份，请填写四位年月")
        note = f"郑商所简码 {code.upper()} 已按最近十年解释为 {year} 年 {month} 月；请核对。"
    else:
        year = 2000 + int(digits[:2])
    yymm = f"{year % 100:02d}{month:02d}"
    symbol = product + yymm
    if market == "CZCE":
        exchange_symbol = product.upper() + str(year % 10) + f"{month:02d}"
    elif market == "CFFEX":
        exchange_symbol = product.upper() + yymm
    else:
        exchange_symbol = symbol
    return {
        "symbol": symbol, "name": product_name + yymm, "product": product,
        "product_name": product_name, "exchange": market,
        "exchange_name": EXCHANGES[market], "exchange_symbol": exchange_symbol,
        "year": year, "month": month, "contract_month": f"{year}-{month:02d}",
        "multiplier": multiplier, "tick_size": tick_size,
        "sina_symbol": product.upper() + yymm,
        "eastmoney_secid": f"{EASTMONEY_MARKETS[market]}.{exchange_symbol}",
        "normalization_note": note, "note": note,
    }


def contract_month_status(contract, *, today=None):
    """Days until the first calendar day of contract delivery month (not expiry).

    A concrete contract code contains the target year/month; none of the
    exchanges share a universal last trading day, so do not claim expiry here.
    """
    from config import now_shanghai
    today = today or now_shanghai().date()
    if isinstance(today, datetime):
        today = today.date()
    year, month = int(contract["year"]), int(contract["month"])
    start = date(year, month, 1)
    days = (start - today).days
    label = f"{year}年{month:02d}月"
    if days > 30:
        return {"days": days, "level": "safe", "display": f"{days} 天", "detail": f"距 {label} · 安全（超过 30 天）"}
    if days > 0:
        return {"days": days, "level": "warn", "display": f"{days} 天", "detail": f"距 {label} · 临近合约月，请注意换月"}
    if today.year == year and today.month == month:
        return {"days": days, "level": "critical", "display": "已进入合约月", "detail": f"{label} · 注意核对交易所最后交易日"}
    return {"days": days, "level": "critical", "display": "合约月份已过", "detail": f"目标 {label} · 请确认该合约是否仍挂牌"}
