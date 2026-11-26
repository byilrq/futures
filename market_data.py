"""行情统一入口（兼容原 imports）。

具体三路实时行情接口、解析、源名称、优先级及具体月份历史接口
已经全部集中到 market_sources.py。以后更新提供商时，只需替换
market_sources.py 并重启 futures-web / futures-worker 两个服务。

这里保留原模块导出名称，避免影响持仓策略、回测以及历史兼容代码。
"""
from market_sources import (  # noqa: F401
    SHANGHAI, SOURCE_ORDER, SOURCE_LABELS, SOURCE_VENDORS, SOURCE_FETCHERS, QuoteError,
    source_options, source_provider_count, parse_sina, parse_eastmoney, parse_emfutures,
    parse_history, fetch_sina, fetch_eastmoney, fetch_emfutures,
    fetch_history, collect_quotes, is_fresh, _http_bytes, _safe_fetch,
    _now, _iso, _number, _quote_base, _verify_czce_decade, _sina_datetime,
    _futures_timestamp, _as_date,
)
