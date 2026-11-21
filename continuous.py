"""Remote-only, causally selected Chinese futures contract-chain backtester.

Real monthly contracts, never a mechanically pasted 'M0' continuous chart,
produce cash P&L.  The previous trading day's open interest selects the NEXT
session's main; roll is executed at that session's close, with simultaneous old
and new monthly closing prices.  Same-day cross-contract spread is NOT P&L.
An additive offset is used exclusively for comparable strategy price lines.

Free source endpoints follow the public Sina and Eastmoney daily protocols.
The service does not assume an expired contract will remain available forever;
missing required candles or absent open interest fail closed with a diagnostic.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
import json
import math
import re
import urllib.parse

from contracts import normalize_contract
from market_data import QuoteError, fetch_history, _http_bytes
from backtest import _options, _positive, _nonnegative, _time, _time_text
from strategy import decimal_text, decimal_value, integer_value, validate_config, evaluate, initial_state

MAX_YEARS = 5
MAX_CANDIDATES = 75
# Known main delivery month families. Other products search all 12 months
# rather than silently assuming 1/5/9 is universal.
MAIN_MONTHS = {
    'm': (1,5,9), 'y': (1,5,9), 'p': (1,5,9),
    'a': (1,5,9), 'b': (1,5,9), 'c': (1,5,9),
    'cs': (1,5,9), 'i': (1,5,9), 'j': (1,5,9),
    'jm': (1,5,9), 'eg': (1,5,9), 'v': (1,5,9),
    'l': (1,5,9), 'pp': (1,5,9), 'rb': (1,5,10),
    'hc': (1,5,10), 'ru': (1,5,9), 'cf': (1,5,9),
    'sr': (1,5,9), 'ta': (1,5,9), 'ma': (1,5,9),
    'oi': (1,5,9), 'rm': (1,5,9), 'fg': (1,5,9),
    'sa': (1,5,9), 'ap': (1,5,10),
}

def _decimal(raw, name):
    try:
        v = decimal_value(raw, name)
    except ValueError as exc:
        raise QuoteError(str(exc)) from exc
    if v <= 0:
        raise QuoteError(f'{name}必须为正数')
    return v

def parse_sina_daily(payload, contract, start, end):
    """Sina InnerFuturesNewService daily JSONP, without evaluation of JS."""
    if isinstance(payload, bytes):
        payload = payload.decode('utf-8-sig', errors='strict')
    if not isinstance(payload, str) or len(payload) > 6_000_000:
        raise QuoteError('新浪历史日线响应无效或过大')
    m = re.search(r'=\s*\(\s*(\[.*?\]|null)\s*\)\s*;?\s*$', payload, re.S | re.I)
    if not m:
        raise QuoteError('新浪历史日线 JSONP 格式发生变化')
    try:
        rows = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise QuoteError('新浪历史日线 JSON 内容无效') from exc
    if rows is None:
        return []
    if not isinstance(rows, list) or len(rows) > 12000:
        raise QuoteError('新浪历史日线记录格式异常')
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            raise QuoteError('新浪历史记录非对象')
        try:
            d = date.fromisoformat(str(row['d'])[:10])
        except (ValueError, KeyError) as exc:
            raise QuoteError('新浪历史 K 线日期无效') from exc
        if not start <= d <= end:
            continue
        if d >= date(contract['year'], contract['month'], 1):
            # Avoid marking contract as tradable through its delivery month.
            continue
        close = _decimal(row.get('c'), '新浪合约收盘价')
        volume = _nonnegative(row.get('v'), '成交量')
        oi = _nonnegative(row.get('p'), '持仓量')
        result = {'time': d.isoformat(), 'date': d.isoformat(),
                  'symbol': contract['symbol'], 'close': decimal_text(close),
                  'volume': decimal_text(volume), 'open_interest': decimal_text(oi),
                  'provider': 'sina'}
        if d in out and out[d] != result:
            raise QuoteError('新浪历史日线包含同日价格冲突')
        out[d] = result
    return [out[d] for d in sorted(out)]


def fetch_sina_daily(contract, start, end, timeout=7):
    # Endpoint shape and documented parameter follow AKShare futures_zh_daily_sina.
    q = urllib.parse.urlencode({'symbol': contract['sina_symbol'], 'type': '2021_04_12'})
    url = ('https://stock2.finance.sina.com.cn/futures/api/jsonp.php/'
           'var%20_FuturesDaily=/InnerFuturesNewService.getDailyKLine?' + q)
    body = _http_bytes(url, timeout=timeout, referer='https://finance.sina.com.cn/')
    return parse_sina_daily(body, contract, start, end)


def fetch_eastmoney_daily(contract, start, end, timeout=7):
    rows = fetch_history(contract, start, end, timeout=timeout)
    output = []
    for r in rows:
        d = date.fromisoformat(str(r['time'])[:10])
        if d >= date(contract['year'], contract['month'], 1):
            continue
        price = _decimal(r.get('price'), '东方财富收盘价')
        volume = _nonnegative(r.get('volume', 0) or 0, '成交量')
        oi = _nonnegative(r.get('open_interest', 0) or 0, '持仓量')
        output.append({'time': d.isoformat(), 'date': d.isoformat(),
                       'symbol': contract['symbol'], 'close': decimal_text(price),
                       'volume': decimal_text(volume), 'open_interest': decimal_text(oi),
                       'provider': 'eastmoney'})
    return output


def contract_candidates(contract, start, end):
    start, end = date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
    if end < start or (end-start).days > 366*MAX_YEARS:
        raise ValueError('主力长周期回测最多支持 5 年，且起始日期不能晚于结束日期')
    product = contract['product']
    months = MAIN_MONTHS.get(product, tuple(range(1, 13)))
    # Include the nearest listed maturities before the first sample and after
    # the last sample. Excluded expired months are never consulted for trade.
    candidates = []
    for year in range(start.year-1, end.year+2):
        for month in months:
            maturity = date(year, month, 1)
            if maturity <= start - timedelta(days=25) or maturity > end + timedelta(days=440):
                continue
            symbol = f'{product}{year%100:02d}{month:02d}'
            candidates.append(normalize_contract(symbol, contract['exchange']))
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError(f'需要获取 {len(candidates)} 个不同交割月，超过单次上限 {MAX_CANDIDATES}；请缩短日期范围')
    return candidates


def download_contract_chain(contract, start, end, *, source='auto', timeout=6, fetcher=None):
    """Fetch actual expiring contracts in parallel; no locally recorded prices.

    ``fetcher`` allows offline fixtures and mocked provider failure testing.
    Failed/nonlisted candidates are omitted, but a missing contract actually
    needed by the execution path causes a fail-closed run_backtest error.
    """
    if source not in ('auto', 'sina', 'eastmoney'):
        raise ValueError('历史行情来源请选择自动、新浪或东方财富')
    start = date.fromisoformat(str(start)[:10])
    end = date.fromisoformat(str(end)[:10])
    candidates = contract_candidates(contract, start, end)
    fetch_start = start - timedelta(days=35)
    call_fetch = fetcher or {'sina': fetch_sina_daily, 'eastmoney': fetch_eastmoney_daily}
    errors = []
    def work(item):
        names = ('sina', 'eastmoney') if source == 'auto' else (source,)
        reasons = []
        for provider in names:
            try:
                # Refuse to confuse error or empty page with valid history.
                data = call_fetch[provider](item, fetch_start, end, timeout)
                if not data:
                    reasons.append(f'{provider} 无记录')
                    continue
                verified = []
                for row in data:
                    if row.get('symbol', '').lower() != item['symbol'].lower():
                        raise ValueError('历史响应合约代码不匹配')
                    if not str(row.get('date')):
                        raise ValueError('历史记录缺少日期')
                    verified.append(row)
                return (item['symbol'], verified, provider, reasons)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                reasons.append(f'{provider}: {str(exc)[:100]}')
        return item['symbol'], [], None, reasons
    result = {}
    sources = defaultdict(int)
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix='futures-history') as pool:
        futures = [pool.submit(work, contract) for contract in candidates]
        for future in as_completed(futures):
            symbol, data, provider, reasons = future.result()
            if data:
                result[symbol] = data
                sources[provider] += 1
            elif len(errors) < 10:
                errors.append(symbol + ': ' + '；'.join(reasons)[:180])
    if not result:
        raise ValueError('无法从免费行情源获取任何有效的具体月份合约历史日线。'
                         '检查 VPS 网络及行情接口限制；' + ' | '.join(errors[:3]))
    return {'contracts': result, 'providers': dict(sources), 'requested': len(candidates),
            'received': len(result), 'errors': errors, 'source': source}


def normalize_chain(bundle):
    """Daily matrix of actual contract bars, strictly no synthetic data repair."""
    out = defaultdict(dict)
    for symbol, bars in bundle['contracts'].items():
        previous = None
        for row in bars:
            if str(row.get('symbol', '')).lower() != symbol.lower():
                raise ValueError('合约历史价格数据混入其他交割月份')
            d = date.fromisoformat(str(row['date']))
            if previous and d <= previous:
                raise ValueError('具体合约日线存在重复或乱序')
            prev = out[d].get(symbol)
            if prev is not None:
                raise ValueError('具体合约同一交易日重复')
            out[d][symbol] = {'price': _decimal(row['close'], '合约收盘价'),
                              'volume': _nonnegative(row.get('volume',0), '成交量'),
                              'oi': _nonnegative(row.get('open_interest',0), '持仓量'),
                              'provider': row.get('provider', 'unknown')}
            previous = d
    return dict(sorted(out.items()))


def _maturity(symbol):
    m = re.fullmatch(r'[a-z]{1,3}(\d{2})(\d{2})', symbol.lower())
    if not m:
        raise ValueError('合约月编码无效')
    return date(2000+int(m.group(1)), int(m.group(2)), 1)


def _choose_rank(scores, today, *, newer_than=None):
    """Rank ONLY yesterday's data by OI (fallback volume) and maturity."""
    valid = []
    for symbol, row in scores.items():
        maturity = _maturity(symbol)
        if maturity <= today or (newer_than is not None and maturity <= newer_than):
            continue
        if row['oi'] <= 0 and row['volume'] <= 0:
            continue
        valid.append((symbol,row))
    if not valid:
        return None
    valid.sort(key=lambda item: (item[1]['oi'],item[1]['volume'], -_maturity(item[0]).toordinal()),reverse=True)
    return valid[0][0]


def _move_grid(state, config, gap):
    """Shift ALL grid lines and observed frontier, never book gap as trading P&L."""
    updated = dict(config)
    state = dict(state)
    for name in ('lower_price','upper_price'):
        updated[name] = decimal_text(Decimal(config[name]) + gap)
    for name in ('last_price','next_buy','next_sell'):
        if state.get(name) is not None:
            state[name] = decimal_text(Decimal(state[name]) + gap)
    # If a far-dated roll would move the whole fixed-grid origin below zero,
    # exact positive-price strategy validation cannot proceed reliably.
    if Decimal(updated['lower_price']) <= 0 or Decimal(updated['upper_price']) <= 0:
        raise ValueError('累计移仓价差令参考价格线非正，无法沿用固定点数阶梯；请缩短回测区间或重新设置起始价格线')
    return state, updated


def _parse_backtest_options(options):
    raw = dict(options or {})
    mode = raw.get('mode','grid')
    if mode not in ('grid','hold'):
        raise ValueError('回测模式须为阶梯策略或长期持有')
    source = raw.get('source','auto')
    if source not in ('auto','sina','eastmoney'):
        raise ValueError('回测历史源不正确')
    roll_days = integer_value(raw.get('roll_days', 15),'交割月提前换月日数',0,90)
    confirm = integer_value(raw.get('roll_confirm', 2), '连续确认交易日数',1,5)
    min_ratio = _positive(raw.get('oi_ratio','1.05'),'主力持仓优势系数')
    if not Decimal('1') <= min_ratio <= Decimal('3'):
        raise ValueError('持仓优势系数应在 1 至 3 之间')
    return raw,mode,roll_days,confirm,min_ratio,source


def run_main_backtest(bundle, contract, cfg, options=None):
    """Long-only grid or 1..N-lot hold; futures daily marked-to-market.

    Only actual monthly closes feed actual P&L. Switching rolls old contract
    into next contract at same-day closes, with fees on BOTH legs. The visible
    strategy price index is back-adjusted by simultaneous contract spread.
    """
    raw, mode, roll_days, confirm, min_ratio, source = _parse_backtest_options(options)
    cfg = validate_config(cfg)
    opt = _options(raw,cfg)
    start = date.fromisoformat(opt['start']) if opt['start'] else min(normalize_chain(bundle))
    end = date.fromisoformat(opt['end']) if opt['end'] else max(normalize_chain(bundle))
    if end < start or (end-start).days > 366*MAX_YEARS:
        raise ValueError('回测日期跨度不得超过 5 年')
    matrix = normalize_chain(bundle)
    dates = [d for d in matrix if start <= d <= end]
    if len(dates) < 2:
        raise ValueError('免费行情源在该日期范围内不足两日有效的实际合约日线')
    old_scores = None
    score_history = []
    current = None
    previous_mark = None
    current_grid = dict(cfg)
    state = initial_state(current_grid)
    offset = Decimal(0)
    cash = Decimal(opt['initial_cash'])
    multiplier = Decimal(cfg['multiplier'])
    slip = Decimal(opt['slippage'])
    fee = Decimal(opt['fee_per_lot'])
    max_lots = opt['max_lots']
    held = 0
    initial_hold = integer_value(raw.get('hold_lots', '1'),'长期持有手数',1,100000)
    if mode == 'hold' and initial_hold > max_lots:
        raise ValueError('长期持有手数不得超过最大仓位')
    if mode == 'grid' and opt['initial_lots']:
        raise ValueError('主力回测请从空仓开始；长期持有请使用「长线持有」模式')
    pnl = Decimal(0)
    fees = Decimal(0)
    slippage_cost = Decimal(0)
    benchmark_pnl = Decimal(0)
    benchmark_lots = 1
    benchmark_fees = Decimal(0)
    benchmark_slippage = Decimal(0)
    peak = cash
    max_dd = max_dd_pct = Decimal(0)
    signals, trades, rolls, series = [],[],[],[]
    skipped = skipped_lots = 0
    observations = 0
    skipped_dates = []
    # Start from lookback days so the first chosen daily main uses T-1 metrics.
    all_dates = sorted(d for d in matrix if d <= end)
    with localcontext() as context:
        context.prec = 45
        for today in all_dates:
            bars = matrix[today]
            if today < start:
                old_scores = bars
                score_history.append((today,bars))
                if len(score_history)>8: score_history.pop(0)
                continue
            if not old_scores:
                old_scores = bars
                score_history.append((today,bars))
                continue
            if current is None:
                candidate = _choose_rank(old_scores,today)
                if not candidate or candidate not in bars:
                    old_scores = bars
                    score_history.append((today,bars))
                    skipped_dates.append(today.isoformat())
                    continue
                current = candidate
                previous_mark = bars[current]['price']
                state = initial_state(current_grid)
                if mode=='hold':
                    held=initial_hold
                    cost = held*(fee+slip*multiplier)
                    fees += held*fee;slippage_cost += held*slip*multiplier;pnl -=cost
                    trades.append({'time':today.isoformat(),'side':'buy','action':'open_long', 'symbol':current,'price':decimal_text(previous_mark),
                                   'lots':held,'fee':decimal_text(held*fee),'realized_pnl':decimal_text(-cost),'reason':'初次建立长线多头','equity':decimal_text(cash+pnl),'end_lots':held})
                benchmark_pnl -= benchmark_lots*(fee+slip*multiplier)
                benchmark_fees+=fee;benchmark_slippage+=slip*multiplier
                state,_,_=evaluate(current_grid,state,previous_mark)
                equity=cash+pnl
                series.append({'time':today.isoformat(),'price':decimal_text(previous_mark-offset),'actual_price':decimal_text(previous_mark),
                               'symbol':current, 'equity':decimal_text(equity),'benchmark_equity':decimal_text(cash+benchmark_pnl)})
                observations+=1
                old_scores=bars
                score_history.append((today,bars))
                continue
            if current not in old_scores:
                raise ValueError(f'{today}：主力合约 {current} 缺少前一交易日持仓量/成交量记录，主力链不完整，已停止回测。')
            if current not in bars:
                # Cannot settle a real held contract without a real daily price.
                raise ValueError(f'{today}：正在持有的主力合约 {current} 缺少当日实际价格，无法安全计算收益。请更换行情源或缩短日期范围。')
            raw_close = bars[current]['price']
            difference = raw_close - previous_mark
            pnl += difference * held * multiplier
            benchmark_pnl += difference * benchmark_lots * multiplier
            old_code = current
            available = [s for s in bars if s in old_scores]
            new = None
            maturity = _maturity(current)
            urgent = (maturity-today).days <= roll_days
            if urgent:
                future = {s:old_scores[s] for s in available if _maturity(s)>maturity}
                new = _choose_rank(future,today,newer_than=maturity)
                if not new:
                    raise ValueError(f'{today}：{current} 临近交割月份，但缺少同时可成交的新月份合约，禁止把旧主力硬接成新价。')
            else:
                for possibility in sorted(available,key=lambda s: _maturity(s)):
                    if _maturity(possibility) <= maturity: continue
                    if old_scores[possibility]['oi'] <= 0 or old_scores[current]['oi'] <= 0: continue
                    if old_scores[possibility]['oi'] < old_scores[current]['oi']*min_ratio: continue
                    # Confirm consecutive prior completed sessions (never today's OI).
                    checks=0
                    for _,prev_day in reversed(score_history):
                        a,b=prev_day.get(possibility),prev_day.get(current)
                        if not a or not b or b['oi']<=0 or a['oi']<b['oi']*min_ratio:break
                        checks += 1
                        if checks >= confirm:break
                    if checks >= confirm:
                        if new is None or old_scores[possibility]['oi']>old_scores[new]['oi']:
                            new=possibility
            if new and new != current:
                gap = bars[new]['price']-raw_close
                # Roll current holdings: old close, new close, fees/slippage for
                # two real legs; never pretend their spread is a daily return.
                roll_fees=fee*2*held
                roll_slip=slip*multiplier*2*held
                pnl-=roll_fees+roll_slip
                fees+=roll_fees;slippage_cost+=roll_slip
                bfee=fee*2*benchmark_lots
                bslip=slip*multiplier*2*benchmark_lots
                benchmark_pnl-=bfee+bslip
                benchmark_fees+=bfee;benchmark_slippage+=bslip
                state,current_grid=_move_grid(state,current_grid,gap)
                offset+=gap
                previous_mark=bars[new]['price']
                current=new
                rolls.append({'time':today.isoformat(),'from':old_code,'to':new,
                              'old_price':decimal_text(raw_close),'new_price':decimal_text(previous_mark),
                              'spread':decimal_text(gap),'lots':held,'fees':decimal_text(roll_fees),
                              'slippage_cost':decimal_text(roll_slip),
                              'reason':'合约月临近强制移仓' if urgent else '前日持仓量连续领先'})
                if held:
                    trades.append({'time':today.isoformat(),'side':'roll','action':'roll','symbol':new,
                                   'price':decimal_text(previous_mark),'lots':held,'fee':decimal_text(roll_fees),
                                   'realized_pnl':decimal_text(-(roll_fees+roll_slip)),'reason':f'{old_code} → {new} 移仓，价差 {decimal_text(gap)}（不计为利润）',
                                   'equity':decimal_text(cash+pnl),'end_lots':held})
            else:
                previous_mark=raw_close
            observed=previous_mark
            state,events,_=evaluate(current_grid,state,observed)
            if mode=='grid':
                for event in events:
                    lots_requested=event['lots']
                    side=event['side']
                    lots=min(lots_requested,max_lots-held if side=='buy' else held)
                    skipped_count=lots_requested-lots
                    signals.append({**event,'time':today.isoformat(),'symbol':current,
                                    'executed_lots':lots,'skipped_lots':skipped_count})
                    if len(signals)>100000: raise ValueError('回测信号过多，请扩大阶梯间隔')
                    if skipped_count:
                        skipped+=1;skipped_lots+=skipped_count
                    if lots<=0: continue
                    cost=lots*(fee+slip*multiplier)
                    held+=lots if side=='buy' else -lots
                    pnl-=cost;fees+=fee*lots;slippage_cost+=slip*multiplier*lots
                    trades.append({'time':today.isoformat(),'side':side,
                                   'action':'open_long' if side=='buy' else 'close_long',
                                   'symbol':current,'price':decimal_text(observed+slip if side=='buy' else observed-slip),
                                   'lots':lots,'fee':decimal_text(lots*fee),'realized_pnl':decimal_text(-cost),
                                   'reason':f'{"买入" if side=="buy" else "卖出"}线 {event["level"]}（价差调整口径）',
                                   'equity':decimal_text(cash+pnl),'end_lots':held})
            equity=cash+pnl
            peak=max(peak,equity)
            dd=max(Decimal(0),peak-equity)
            max_dd=max(max_dd,dd)
            if peak>0:max_dd_pct=max(max_dd_pct,dd/peak*100)
            series.append({'time':today.isoformat(),'price':decimal_text(observed-offset),
                           'actual_price':decimal_text(observed),'symbol':current,
                           'equity':decimal_text(equity), 'benchmark_equity':decimal_text(cash+benchmark_pnl)})
            observations+=1
            old_scores=bars
            score_history.append((today,bars))
            if len(score_history)>8:score_history.pop(0)
        if observations<2:
            raise ValueError('远端历史数据没有形成连续两日可确认的主力合约；请更换免费源或缩短回测日期')
        if not series or date.fromisoformat(series[0]['time']) > start+timedelta(days=45):
            raise ValueError('所选开始日期附近无可核对的主力合约数据，禁止只用末尾短样本冒充全区间回测')
        if date.fromisoformat(series[-1]['time']) < end-timedelta(days=14):
            raise ValueError('所选结束日期附近远端未提供主力日线；请缩短日期范围或切换数据源')
        if mode=='grid' and held and opt['initial_lots']:
            raise ValueError('主力链回测不支持外部期初仓位')
        trades_count=sum(1 for t in trades if t['action'] != 'roll')
        return_pct=pnl/cash*100
        days=max(1,(date.fromisoformat(series[-1]['time'])-date.fromisoformat(series[0]['time'])).days)
        ann=((cash+pnl)/cash)**(Decimal('365.25')/days)-1 if cash+pnl>0 else None
        result={'stats':{'points':observations,'signals':len(signals),'trades':trades_count,'skipped':skipped,
                         'skipped_lots':skipped_lots,'end_lots':held,
                         'realized_pnl':decimal_text(pnl),'unrealized_pnl':'0',
                         'fees':decimal_text(fees),'slippage_cost':decimal_text(slippage_cost),
                         'total_pnl':decimal_text(pnl),'max_drawdown':decimal_text(max_dd),
                         'max_drawdown_pct':decimal_text(max_dd_pct),
                         'ending_equity':decimal_text(cash+pnl),'return_pct':decimal_text(return_pct),
                         'annualized_return_pct':decimal_text(ann*100) if ann is not None else '—',
                         'buy_hold_pnl':decimal_text(benchmark_pnl), 'buy_hold_lots':1,
                         'rolls':len(rolls),'initial_cash':decimal_text(cash),
                         'initial_lots':initial_hold if mode=='hold' else 0,
                         'start':series[0]['time'],'end':series[-1]['time']},
                'signals':signals,'trades':trades,'rolls':rolls,'series':series,
                'warnings':['主力选择仅使用前一已完成交易日的持仓量（连续确认）；不使用当日收盘后的未来排名。',
                            '跨月先按旧合约收盘价逐日盯市，移仓时分别记录旧平与新开；新旧合约的同期价差不算收益。',
                            '价差累计平移仅用于价格阶梯信号；实际盈亏、移仓和手续费使用两个具体月份合约的原始收盘价。',
                            '仅据免费历史合约样本估算主力，非交易所官方主力序列；缺失合约可能导致可选范围偏差。',
                            '使用实际合约收盘价估算逐日权益；未使用交易所结算价、未模拟保证金、强平、盘中路径、涨跌停和盘口冲击。',
                            '基准为连续持有 1 手并付出同样的开仓/换月费用；不是未复权主连的价格涨跌。'],
                'config':{'strategy':cfg,'options':opt,'execution':'actual_contract_close_mark_to_market',
                          'position_mode':'long_only','roll_policy':{'roll_days':roll_days,
                          'confirm_days':confirm,'min_ratio':decimal_text(min_ratio)},'mode':mode}}
        result['source_info']={'source':source, 'providers':bundle.get('providers',{}),
                               'requested':bundle.get('requested'),'received':bundle.get('received'),
                               'errors':bundle.get('errors',[])[:6], 'skipped_dates':skipped_dates[:10]}
        if skipped_dates:
            result['warnings'].append(f'历史初段有 {len(skipped_dates)} 天缺少主力交集，未假定交易成交。')
        return result


def starting_main_price(bundle, start, end):
    """Find a causal first tradable daily main close for auto strategy lines."""
    matrix=normalize_chain(bundle)
    begin=date.fromisoformat(str(start)[:10])
    finish=date.fromisoformat(str(end)[:10])
    prior=None
    for today,bars in matrix.items():
        if today>finish:break
        if today < begin:
            prior=bars
            continue
        if prior:
            symbol=_choose_rank(prior,today)
            if symbol and symbol in bars:
                return {'time':today.isoformat(),'symbol':symbol,
                        'price':decimal_text(bars[symbol]['price'])}
        prior=bars
    raise ValueError('没有足够的连续合约成交量/持仓量历史，无法确定首日主力。')
