#!/usr/bin/env python3
"""
币安山寨合约——异动币多空策略 v4.7 实盘版
5x 逐仓 | 单仓5U保证金 | 市价开仓 + 止损STOP_MARKET + 止盈限价单
"""

import ccxt
import json
import time
import os
import sys
import math
import urllib.request
import urllib.parse
from datetime import datetime

# ===== Telegram 推送配置 =====
TG_BOT_TOKEN = '8372471397:AAHG3KjXOog4D3MUALRZuUyqkSayN0YbNz0'
TG_CHAT_ID = '6155212881'

def send_telegram(msg):
    """发送消息到Telegram（纯HTTP，不依赖大模型）"""
    try:
        url = f'https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage'
        data = urllib.parse.urlencode({'chat_id': TG_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}).encode()
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/x-www-form-urlencoded'})
        urllib.request.urlopen(req, timeout=10)
    except:
        pass

# ===== 配置 =====
WORKSPACE = '/home/ubuntu/.openclaw/workspace'
CONFIG_PATH = os.path.join(WORKSPACE, 'config', 'binance.json')
STATE_PATH = os.path.join(WORKSPACE, 'data', 'trade_state.json')

LEVERAGE = 5          # 固定杠杆
MARGIN_PER_TRADE = 5  # 单仓保证金5 USDT
EXCLUDE = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
MIN_VOLUME_24H = 100000  # 最低24h成交量 USDT

# ===== 初始化币安API =====
with open(CONFIG_PATH) as f:
    cfg = json.load(f)

b = ccxt.binance({
    'apiKey': cfg['apiKey'],
    'secret': cfg['secretKey'],
    'options': {'defaultType': 'future'}
})
b.load_markets()


def get_precision(tick_str):
    """从tickSize字符串获取小数位数，如 '0.00001' -> 5"""
    s = tick_str.rstrip('0')
    if '.' in s:
        return len(s.split('.')[-1])
    return 0


def round_to_tick(val, tick_size, price_prec):
    """向下取整到tickSize倍数"""
    mult = 10 ** price_prec
    return round(math.floor(val * mult) / mult, price_prec)


def round_to_step(val, step_size, amt_prec):
    """向下取整到stepSize倍数，用Decimal避免浮点误差"""
    mult = 10 ** amt_prec
    return round(math.floor(val * mult) / mult, amt_prec)


def get_market_precision(symbol):
    """获取币种的价格/数量精度"""
    m = b.markets.get(symbol)
    if not m:
        return 8, 0, 1, 0, 0.0001, 0.1
    filters = {f['filterType']: f for f in m['info']['filters']}
    tick_size = float(filters['PRICE_FILTER']['tickSize'])
    step_size = float(filters['LOT_SIZE']['stepSize'])
    price_prec = get_precision(filters['PRICE_FILTER']['tickSize'])
    amt_prec = get_precision(filters['LOT_SIZE']['stepSize'])
    min_qty = float(filters['LOT_SIZE'].get('minQty', step_size))
    return tick_size, price_prec, step_size, amt_prec, min_qty




# 日志文件路径
LOG_PATH = 'data/trade_log.txt'

def log_to_file(action, symbol, side, detail):
    """记录开仓/平仓/止盈/止损等操作日志"""
    try:
        ts = datetime.now().strftime('%m-%d %H:%M:%S')
        entry = '[{}] {:8s} {:15s} {:5s} {}'.format(ts, action, symbol, side, detail)
        with open(LOG_PATH, 'a') as f:
            f.write(entry + '\n')
        # 同时打印到控制台
        print(entry)
    except:
        pass


def load_trade_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {'open_positions': [], 'history': []}


def save_trade_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, 'w') as f:
        json.dump(state, f, indent=2, default=str)


# ── 持仓查询 ──────────────────────────────────────────────
def get_open_positions():
    """获取当前所有持仓（positionAmt != 0）"""
    try:
        positions = b.fetch_positions()
        open_pos = []
        for p in positions:
            info = p.get('info', {})
            amt = float(info.get('positionAmt', 0) or 0)
            if abs(amt) > 0:
                ps = info.get('positionSide', '')
                direction = ps if ps != 'BOTH' else ('LONG' if amt > 0 else 'SHORT')
                open_pos.append({
                    'symbol': p['symbol'],
                    'side': direction,
                    'size': abs(amt),
                    'entry_price': float(info.get('entryPrice', 0)),
                    'liq_price': float(info.get('liquidationPrice', 0)),
                    'unrealized_pnl': float(info.get('unrealizedProfit', 0)),
                    'margin': float(p.get('collateral', 0)),
                })
        return open_pos
    except Exception as e:
        print('获取持仓失败: {}'.format(e), file=sys.stderr)
        return []


def get_open_algos():
    """获取当前所有Algo条件单（止损/止盈）"""
    try:
        algos = b.fapiPrivateGetOpenAlgoOrders({})
        result = []
        for a in algos:
            result.append({
                'symbol': a['symbol'],
                'type': a['orderType'],
                'side': a['side'],
                'pos_side': a['positionSide'],
                'qty': float(a['quantity']),
                'stop_price': float(a['triggerPrice']),
                'algo_id': a['algoId'],
                'status': a['algoStatus'],
            })
        return result
    except Exception as e:
        print('获取条件单失败: {}'.format(e), file=sys.stderr)
        return []


# ── 杠杆/保证金设置 ──────────────────────────────────────
def set_leverage(mid):
    try:
        b.fapiPrivatePostLeverage({'symbol': mid, 'leverage': LEVERAGE})
    except Exception:
        pass


def set_margin_mode(mid):
    try:
        b.fapiPrivatePostMarginType({'symbol': mid, 'marginType': 'ISOLATED'})
    except Exception as e:
        if 'already' not in str(e).lower():
            print('设置逐仓失败 {}: {}'.format(mid, e), file=sys.stderr)


def check_can_trade(symbol):
    """检查是否可交易（已签署协议等）"""
    try:
        ticker = b.fetch_ticker(symbol)
        return float(ticker.get('last', 0) or ticker.get('close', 0)) > 0
    except Exception:
        return False


# ── Algo订单操作 ──────────────────────────────────────────
def cancel_all_algos_for_symbol(symbol):
    """取消指定币种的所有条件单"""
    mid = symbol.replace('/USDT:USDT', 'USDT')
    try:
        b.fapiPrivateDeleteAlgoOpenOrders({'symbol': mid})
        return True
    except Exception:
        return False


def place_algo_order(symbol, side, order_type, qty, stop_price, pos_side):
    """通过ccxt create_order (会自动路由到Algo API) 下单条件单"""
    try:
        order = b.create_order(
            symbol, order_type, side, qty,
            params={'stopPrice': stop_price, 'positionSide': pos_side}
        )
        return order.get('id', '?')
    except Exception as e:
        raise e


# ── 开仓 ──────────────────────────────────────────────────
def open_position(mid, symbol, side, entry_price, stop_price, tp1_price, tp2_price, fr_val, reasons, pos=None):
    """
    完整开仓流程：
    1. 设杠杆/逐仓 → 2. 判断是否等回调/反弹再进场
    3. 挂Algo条件单(止损STOP_MARKET) → 4. 挂LIMIT止盈单(TP1/TP2)
    """
    market_info = b.market(symbol)
    mkt_filters = {f['filterType']: f for f in market_info['info']['filters']}
    tick_size = float(mkt_filters['PRICE_FILTER']['tickSize'])
    step_size = float(mkt_filters['LOT_SIZE']['stepSize'])
    amt_prec = int(market_info['precision']['amount'])
    price_prec = int(market_info['precision']['price'])
    if price_prec < 1 or price_prec > 8:  # fallback for float/weird prec
        # try to derive from tickSize filter
        m = b.markets.get(symbol)
        if m and m.get('info',{}).get('filters'):
            for f in m['info']['filters']:
                if f.get('filterType') == 'PRICE_FILTER':
                    ts = f.get('tickSize','0.0001')
                    s = ts.rstrip('0')
                    if '.' in s:
                        price_prec = len(s.split('.')[-1])
                    break
        if price_prec < 1:
            price_prec = 8
    if amt_prec < 1 or amt_prec > 8:
        m = b.markets.get(symbol)
        if m and m.get('info',{}).get('filters'):
            for f in m['info']['filters']:
                if f.get('filterType') == 'LOT_SIZE':
                    ss = f.get('stepSize','1')
                    s = ss.rstrip('0')
                    if '.' in s:
                        amt_prec = len(s.split('.')[-1])
                    break
        if amt_prec < 1:
            amt_prec = 0
    min_amt = market_info['limits']['amount']['min']

    # 数量 = 保证金*杠杆 / 价格
    amount = MARGIN_PER_TRADE * LEVERAGE / entry_price
    amount_rounded = max(round_to_step(amount, step_size, amt_prec), min_amt)
    actual_margin = amount_rounded * entry_price / LEVERAGE

    side_buy = 'buy' if side == 'LONG' else 'sell'
    pos_side_param = 'LONG' if side == 'LONG' else 'SHORT'

    print('  {} @ {} ({} U保证金, {}x)'.format(symbol, amount_rounded, round(actual_margin, 2), LEVERAGE))

    set_leverage(mid)
    set_margin_mode(mid)

    try:
        # 1. 判断是否等回调/反弹后再开仓
        # 做多时：1h涨幅>3%，涨太快，等回调再进
        # 做多：布林pos>70%（靠近上轨），等回撤到50%~70%之间再进
        # 做空：布林pos<30%（靠近下轨），等反弹到30%~50%之间再空
        if pos is not None:
            if side == 'LONG' and pos > 70:
                log_to_file('开仓跳过', symbol, side, '布林pos={:.0f}%>70%，等回调到50%~70%再开'.format(pos))
                print('  布林pos={:.0f}%>70%，等回调到50%~70%再开'.format(pos))
                return 'skipped'
            if side == 'SHORT' and pos < 30:
                log_to_file('开仓跳过', symbol, side, '布林pos={:.0f}%<30%，等反弹到30%~50%再空'.format(pos))
                print('  布林pos={:.0f}%<30%，等反弹到30%~50%再空'.format(pos))
                return 'skipped'
        
        # 2. 市价开仓
        order = b.create_market_order(symbol, side_buy, amount_rounded,
                                      params={'positionSide': pos_side_param})
        oid = order.get('id', '?')
        filled_price = float(order.get('average', entry_price) or entry_price)
        log_to_file('开仓', symbol, side, '市价开仓 price={:.8f} size={} id={} margin={:.2f}U'.format(
            filled_price, amount_rounded, oid, actual_margin))
        print('  开仓成功 id={} 均价={}'.format(oid, filled_price))

        # 2. 挂止损条件单（Algo API STOP_MARKET）
        sl_trigger = round_to_tick(stop_price, tick_size, price_prec)
        if side == 'LONG':
            if sl_trigger < filled_price:
                place_algo_order(symbol, 'sell', 'STOP_MARKET', amount_rounded, sl_trigger, 'LONG')
                log_to_file('开仓', symbol, 'LONG', '止损 STOP_MARKET {:.8f} x{}'.format(sl_trigger, amount_rounded))
                print('  止损挂单: {}'.format(sl_trigger))
        else:
            if sl_trigger > filled_price:
                place_algo_order(symbol, 'buy', 'STOP_MARKET', amount_rounded, sl_trigger, 'SHORT')
                log_to_file('开仓', symbol, 'SHORT', '止损 STOP_MARKET {:.8f} x{}'.format(sl_trigger, amount_rounded))
                print('  止损挂单: {}'.format(sl_trigger))

        # 3. 挂LIMIT止盈基础单（两段50%，timeInForce=GTC）
        tp1_price_rnd = round_to_tick(tp1_price, tick_size, price_prec)
        tp2_price_rnd = round_to_tick(tp2_price, tick_size, price_prec)
        tp1_qty = max(round_to_step(amount_rounded * 0.5, step_size, amt_prec), min_amt)
        tp2_qty = round_to_step(amount_rounded - tp1_qty, step_size, amt_prec)
        if tp2_qty < min_amt:
            tp2_qty = 0
        # 注意：LIMIT止盈单在双向模式下必须带timeInForce=GTC才能稳定挂住
        ps = 'LONG' if side == 'LONG' else 'SHORT'
        tp_side = 'sell' if side == 'LONG' else 'buy'
        if tp1_qty > 0:
            if (side == 'LONG' and tp1_price_rnd > filled_price) or (side == 'SHORT' and tp1_price_rnd < filled_price):
                b.create_order(symbol, 'LIMIT', tp_side, tp1_qty, price=tp1_price_rnd,
                               params={'positionSide': ps, 'timeInForce': 'GTC'})
                log_to_file('开仓', symbol, side, '止盈1 LIMIT {:.8f} x{}'.format(tp1_price_rnd, tp1_qty))
                tp1_pct = abs(tp1_price_rnd / filled_price - 1) * 100
                print('  止盈1(50%+{:.0f}%): {} x{}'.format(tp1_pct, tp1_price_rnd, tp1_qty))
            time.sleep(0.1)
        if tp2_qty > 0:
            if (side == 'LONG' and tp2_price_rnd > filled_price) or (side == 'SHORT' and tp2_price_rnd < filled_price):
                b.create_order(symbol, 'LIMIT', tp_side, tp2_qty, price=tp2_price_rnd,
                               params={'positionSide': ps, 'timeInForce': 'GTC'})
                log_to_file('开仓', symbol, side, '止盈2 LIMIT {:.8f} x{}'.format(tp2_price_rnd, tp2_qty))
                tp2_pct = abs(tp2_price_rnd / filled_price - 1) * 100
                print('  止盈2(50%+{:.0f}%): {} x{}'.format(tp2_pct, tp2_price_rnd, tp2_qty))
            time.sleep(0.1)

        # 4. 记录到状态文件
        state = load_trade_state()
        state['open_positions'].append({
            'symbol': symbol,
            'side': side,
            'entry_price': filled_price,
            'size': amount_rounded,
            'stop_price': stop_price,
            'tp1_price': tp1_price,
            'tp2_price': tp2_price,
            'margin': actual_margin,
            'open_time': datetime.now().timestamp(),
            'reasons': reasons,
            'fr_val': fr_val,
            'order_id': oid,
        })
        save_trade_state(state)
        return 'opened'

    except Exception as e:
        log_to_file('开仓失败', symbol, side, str(e)[:60])
        print('  开仓失败: {}'.format(e), file=sys.stderr)
        return False


# ── 平仓 ──────────────────────────────────────────────────
def close_position(symbol, side):
    """市价平仓指定方向持仓，取消对应的风控单"""
    try:
        pos_list = b.fetch_positions()
        for p in pos_list:
            if p['symbol'] != symbol:
                continue
            info = p.get('info', {})
            amt = float(info.get('positionAmt', 0) or 0)
            if abs(amt) == 0:
                continue
            ps = info.get('positionSide', '')
            d = ps if ps != 'BOTH' else ('LONG' if amt > 0 else 'SHORT')
            if d != side:
                continue
            
            side_close = 'sell' if side == 'LONG' else 'buy'
            pos_side_param = 'LONG' if side == 'LONG' else 'SHORT'
            
            # 平仓：删除该币种所有条件单并取消所有限价单（该币种已确定平仓，两方向都不应再持有）
            mid = symbol.replace('/USDT:USDT', 'USDT')
            try:
                b.fapiPrivateDeleteAlgoOpenOrders({'symbol': mid})
            except: pass
            try:
                ords = b.fetch_open_orders(symbol)
                for o in ords:
                    b.cancel_order(o['id'], symbol)
            except: pass
            
            # 市价平仓
            order = b.create_market_order(symbol, side_close, abs(amt),
                                           params={'positionSide': pos_side_param})
            price = float(order.get('average', 0))
            log_to_file('平仓', symbol, side, '市价平仓 price={:.8f} size={} id={} pnl={:.4f}'.format(
                price, abs(amt), order.get('id','?'), float(info.get('unrealizedProfit',0))))
            print('  平仓 {} side={} price={} id={}'.format(symbol, side, price, order.get('id','?')))
            
            # 从状态文件删除
            try:
                state = load_trade_state()
                state['open_positions'] = [p for p in state['open_positions'] if p.get('symbol') != symbol or p.get('side') != side]
                save_trade_state(state)
            except: pass
            
            return True
    except Exception as e:
        print('  平仓失败 {}: {}'.format(symbol, e), file=sys.stderr)
        return False
    return False


# ── 已有持仓重新分析 ────────────────────────────────────
def review_existing_positions(positions):
    """每轮扫描：已有持仓只看费率变化和量能变化，决定是否提前平仓"""
    closed = []
    held = []
    for p in positions:
        sym = p['symbol']
        mid = sym.replace('/USDT:USDT', 'USDT')
        side = p['side']
        
        try:
            # 拉取最新资金费率
            fr = b.fetch_funding_rate(sym)
        except Exception:
            held.append(sym)
            continue
        
        fr_val = float(fr['fundingRate']) * 100
        
        reasons = []
        should_close = False
        
        # 做多持仓检查：只看资金费率
        if side == 'LONG':
            if fr_val > -0.05:
                should_close = True
                reasons.append('费{:.2f}%已不构成做多支撑'.format(fr_val))
        
        # 做空持仓检查：只看资金费率
        if side == 'SHORT':
            if fr_val < 0.05:
                should_close = True
                reasons.append('费{:.2f}%已不构成做空支撑'.format(fr_val))
        
        if should_close:
            if close_position(sym, side):
                reason_str = '|'.join(reasons[:2])
                closed.append({'symbol': sym, 'side': side, 'reason': reason_str, 'entry': p['entry_price']})
                print('  ⚠️ {} {} 信号减弱: {}，主动平仓'.format(sym, side, reason_str))
        else:
            held.append(sym)
    
    return closed, held



# ── 信号分析 ──────────────────────────────────────────────
def analyze_symbol(mid, sym):
    """分析单个合约，返回信号（如有）"""
    try:
        # 先快速查资金费率，不满足硬性条件直接跳过
        fr = b.fetch_funding_rate(sym)
    except Exception:
        return None
    
    fr_val = float(fr['fundingRate']) * 100
    # 硬性过滤：|费率| < 0.1% 直接跳过，不查K线和ticker
    if abs(fr_val) < 0.15:
        time.sleep(0.01)
        return None
    
    try:
        ohlcv_1h = b.fetch_ohlcv(mid, '1h', limit=30)
        if len(ohlcv_1h) < 10:
            return None
        ohlcv_15m = b.fetch_ohlcv(mid, '15m', limit=12)
        t = b.fetch_ticker(mid)
        time.sleep(0.02)
    except Exception:
        return None

    c1 = [o[4] for o in ohlcv_1h]
    o1 = [o[1] for o in ohlcv_1h]
    h1 = [o[2] for o in ohlcv_1h]
    l1 = [o[3] for o in ohlcv_1h]
    v1 = [o[5] for o in ohlcv_1h]

    last = c1[-1]

    # 24h成交量过滤
    vol_24h = t.get('quoteVolume', 0) or 0
    if vol_24h < MIN_VOLUME_24H:
        return None

    pct_24h = t.get('percentage', 0) or 0
    high_24h = t.get('high', last)
    low_24h = t.get('low', last)
    # 近3根1小时K线平均涨幅（涨速平滑判断，避免单根K线毛刺）
    ret_3h_avg = 0.0
    for i in range(min(3, len(c1)-1)):
        idx = -(i+2)
        if c1[idx] > 0 and c1[idx-1] > 0:
            ret_3h_avg += (c1[idx] / c1[idx-1] - 1)
    ret_3h_avg = (ret_3h_avg / min(3, len(c1)-1)) * 100 if min(3, len(c1)-1) > 0 else 0
    ret_1h = ret_3h_avg  # ret_1h变量复用为近3h平均涨幅
    ret_6h = (c1[-1] / c1[-7] - 1) * 100 if len(c1) > 7 else 0
    ret_12h = (c1[-1] / c1[-13] - 1) * 100 if len(c1) > 13 else 0
    ret_30h = (c1[-1] / c1[0] - 1) * 100 if len(c1) > 1 else 0

    vol_prev5 = sum(v1[-6:-1]) / 5 if sum(v1[-6:-1]) > 0 else 1
    vr = v1[-1] / vol_prev5
    vol_trend = sum(v1[-5:]) / sum(v1[-10:-5]) if sum(v1[-10:-5]) > 0 else 1

    # 计算布林通道（20根1小时K线）
    bb_period = min(20, len(c1))
    if bb_period > 1:
        bb_sma = sum(c1[-bb_period:]) / bb_period
        bb_var = sum((x - bb_sma) ** 2 for x in c1[-bb_period:]) / bb_period
        bb_std = math.sqrt(bb_var)
        bb_upper = bb_sma + 2 * bb_std
        bb_lower = bb_sma - 2 * bb_std
        pos = (last - bb_lower) / (bb_upper - bb_lower) * 100 if bb_upper > bb_lower else 50
    else:
        pos = 50

    body = abs(c1[-1] - o1[-1]) / o1[-1] * 100 if o1[-1] > 0 else 0
    upper_shadow = (h1[-1] - max(c1[-1], o1[-1])) / o1[-1] * 100 if o1[-1] > 0 else 0
    lower_shadow = (min(c1[-1], o1[-1]) - l1[-1]) / o1[-1] * 100 if o1[-1] > 0 else 0
    is_up = c1[-1] > o1[-1]

    rets = [(c1[-j] / c1[-j - 1] - 1) * 100 for j in range(1, 6)]
    ret_max = max(rets)
    ret_min = min(rets)
    ret_recent = rets[0]

    up_count = sum(1 for j in range(1, 6) if c1[-j] > c1[-j - 1])
    down_count = 5 - up_count

    # ── 费率过滤 ──
    if abs(fr_val) < 0.15:
        return None

    ls = 0
    lr = []
    ss = 0
    sr = []

    # ── 量能趋势 ──
    if vol_trend > 3:
        ls += 3
        lr.append('量能暴增{:.1f}x'.format(vol_trend))
        ss -= 2
        sr.append('量暴动未竭-{:.1f}x'.format(vol_trend))
    elif vol_trend > 2:
        ls += 2
        lr.append('量能持续{:.1f}x'.format(vol_trend))
        ss -= 1
        sr.append('量放动未衰-{:.1f}x'.format(vol_trend))
    elif vol_trend > 1.5:
        ls += 1
        lr.append('量能放大{:.1f}x'.format(vol_trend))
        ss -= 0.5
        sr.append('量放动未竭-{:.1f}x'.format(vol_trend))

    # ── 基础费率评分 ──
    if fr_val < -0.15:
        ls += 3
        lr.append('空头恐{:.2f}%'.format(abs(fr_val)))
    elif fr_val < -0.05:
        ls += 1.5
        lr.append('空头拥{:.2f}%'.format(abs(fr_val)))

    if fr_val > 0.15:
        ss += 3
        sr.append('多头过{:.2f}%'.format(fr_val))
    elif fr_val > 0.05:
        ss += 1.5
        sr.append('多头拥{:.2f}%'.format(fr_val))

    # ── 做多信号细项 ──
    if fr_val < -0.15:
        if is_up and vr > 1.2:
            ls += 2
            lr.append('轧空中涨+放量')
        if pos < 35:
            if ret_min < -1 and abs(ret_recent) < abs(ret_min) * 0.5:
                ls += 3
                lr.append('跌衰减{:.1f}%<峰{:.1f}%'.format(abs(ret_recent), abs(ret_min)))
            if vr < 0.8:
                ls += 2
                lr.append('低位缩量')
            if is_up:
                ls += 2
                lr.append('低位出阳线')
            if ret_6h > -3:
                ls += 1.5
                lr.append('费{:.2f}%跌不动'.format(fr_val))
            bodies = [abs(c1[-j] - o1[-j]) / o1[-j] * 100 if o1[-j] > 0 else 0 for j in range(1, 5)]
            if len(bodies) >= 2 and bodies[0] < bodies[-1] * 0.7:
                ls += 1
                lr.append('实体缩=衰竭')
            if max(rets) < 3 and min(rets) > -3:
                ls += 1
                lr.append('低位窄横')
            if down_count >= 3 and is_up:
                ls += 1
                lr.append('连跌{}转阳'.format(down_count))
            if lower_shadow > 1.5:
                ls += 1
                lr.append('下影有承接')
        if ret_6h > 8 and ret_12h > 5:
            ls += 1
            lr.append('动量加速')
        if ret_30h < 0:
            ls -= 1
        elif ret_30h > 15 and pct_24h > 3:
            ls += 1
            lr.append('持续涨{:.0f}%'.format(ret_30h))

    # ── 做空信号细项 ──
    if fr_val > 0.15:
        if pos > 65:
            if is_up and ret_max > 2 and ret_recent < ret_max * 0.5:
                ss += 3
                sr.append('涨衰减{:.1f}%<峰{:.1f}%'.format(ret_recent, ret_max))
            if not is_up:
                ss += 3
                sr.append('高位转阴实体{:.1f}%'.format(body))
            elif upper_shadow > body * 0.7:
                ss += 2
                sr.append('高上引=抛压')
            if vr < 0.8:
                ss += 2
                sr.append('高位缩量')
            if ret_6h < 3:
                ss += 1.5
                sr.append('费{:.2f}%涨不动'.format(fr_val))
            if max(rets) < 2 and min(rets) > -2:
                ss += 1
                sr.append('高位窄横')
            if up_count >= 3 and not is_up:
                ss += 1
                sr.append('连涨{}转阴'.format(up_count))
            bodies = [abs(c1[-j] - o1[-j]) / o1[-j] * 100 if o1[-j] > 0 else 0 for j in range(1, 5)]
            if len(bodies) >= 2 and bodies[0] < bodies[-1] * 0.7:
                ss += 1
                sr.append('实体缩=衰')
        if not is_up and vr > 1.5:
            ss += 2
            sr.append('踩踏中跌+放量')
        if not is_up and vr > 2 and pos > 75:
            ss += 2
            sr.append('高位放量滞=派发')
        if ret_6h < -8 and ret_12h < -5:
            ss += 1
            sr.append('动量加速')
        if ret_30h > 0 and pct_24h < -5:
            ss -= 1
        elif ret_30h < -15 and pct_24h < -3:
            ss += 1
            sr.append('持续跌{:.0f}%'.format(ret_30h))

    # ── 费率方向与信号方向抑制 ──
    if fr_val > 0 and ls > 0:
        ls -= 1
    if fr_val < 0 and ss > 0:
        ss -= 1

    # ── 大趋势惩罚 ──
    if fr_val > 0 and ret_30h > 5:
        ss -= 3
        sr.append('大势向上-3')
    elif fr_val > 0 and ret_30h > 2:
        ss -= 1.5
        sr.append('势偏多-1.5')
    if fr_val < 0 and ret_30h < -5:
        ls -= 3
        lr.append('大势向下-3')

    # ── 最终信号判定 ──
    d = None
    if fr_val < -0.15 and ls >= 6.0 and ls >= ss + 2.0:
        d = 'LONG'
    elif fr_val > 0.15 and ss >= 6.0 and ss >= ls + 2.0:
        d = 'SHORT'
    if d is None:
        return None

    # ── 风控计算（止损/止盈） ──
    if d == 'LONG':
        entry_price = last
        base_sl = 10
        # 费率调整：|费率|>=0.15% → +2，0.10%~0.15% → +1（过0.1%门槛但不算极端的给+1更合理）
        sl_adj = (2 if abs(fr_val) >= 0.15 else 1) + (1 if vol_trend > 5 else -1 if vol_trend < 1.5 else 0)
        sl_pct = base_sl + sl_adj
        sl_price = entry_price * (1 - sl_pct / 100)
        # 止盈跟随止损比例动态调整
        tp1_mult = 1.5
        tp2_mult = 2.5
        tp1_pct = sl_pct * tp1_mult
        tp2_pct = sl_pct * tp2_mult
        tp1_price = entry_price * (1 + tp1_pct / 100)
        tp2_price = entry_price * (1 + tp2_pct / 100)
        reasons = ' | '.join(lr[:5])
        return {
            'symbol': sym, 'direction': d, 'score': round(ls, 1),
            'price': last, 'entry_price': entry_price,
            'stop_price': sl_price, 'tp1_price': tp1_price, 'tp2_price': tp2_price,
            'sl_pct': sl_pct, 'reasons': reasons,
            'pct_24h': pct_24h, 'fr_val': fr_val, 'pos': pos,
            'vr': vr, 'vol_trend': vol_trend,
            'ls': round(ls, 1),
            'ss': round(ss, 1),
            'ret_1h': ret_1h,
        }
    else:
        entry_price = last
        base_sl = 10
        # 费率调整：|费率|>=0.15% → +2，0.10%~0.15% → +1（过0.1%门槛但不算极端的给+1更合理）
        sl_adj = (2 if abs(fr_val) >= 0.15 else 1) + (1 if vol_trend > 5 else -1 if vol_trend < 1.5 else 0)
        sl_pct = base_sl + sl_adj
        sl_price = entry_price * (1 + sl_pct / 100)
        # 止盈跟随止损比例动态调整
        tp1_mult = 1.5
        tp2_mult = 2.5
        tp1_pct = sl_pct * tp1_mult
        tp2_pct = sl_pct * tp2_mult
        tp1_price = entry_price * (1 - tp1_pct / 100)
        tp2_price = entry_price * (1 - tp2_pct / 100)
        reasons = ' | '.join(sr[:5])
        return {
            'symbol': sym, 'direction': d, 'score': round(ss, 1),
            'price': last, 'entry_price': entry_price,
            'stop_price': sl_price, 'tp1_price': tp1_price, 'tp2_price': tp2_price,
            'sl_pct': sl_pct, 'reasons': reasons,
            'pct_24h': pct_24h, 'fr_val': fr_val, 'pos': pos,
            'vr': vr, 'vol_trend': vol_trend,
            'ls': round(ls, 1),
            'ss': round(ss, 1),
            'ret_1h': ret_1h,
        }


# ── 主入口 ──────────────────────────────────────────────
def main():
    from datetime import datetime
    now = datetime.now()
    timestamp = now.strftime('%m-%d %H:%M')

    print('{} 山寨异动策略扫描 [实盘交易版 v4.2]'.format(timestamp))
    print('   杠杆{}x 逐仓 | 单仓保证金{} U'.format(LEVERAGE, MARGIN_PER_TRADE))
    print()

    # 查余额（用于推送）
    try:
        bal = b.fetch_balance()
    except:
        bal = {'total': {'USDT': 0}, 'free': {'USDT': 0}}

    existing_positions = get_open_positions()
    existing_symbols_side = {(p['symbol'], p['side']) for p in existing_positions}
    existing_symbols = {p['symbol'] for p in existing_positions}

    closed_positions, held_positions = [], []

    if existing_positions:
        print('当前已有 {} 个持仓:'.format(len(existing_positions)))
        for p in existing_positions:
            print('  {} {} 入场{} 大小{} 盈亏{:+.2f}'.format(
                p['symbol'].ljust(20), p['side'].ljust(5),
                p['entry_price'], p['size'], p['unrealized_pnl']))
        print()

        # ── 先清理孤儿仓 ──
        print('--- 清理孤儿止损止盈 ---')
        cleaned_algo = 0
        cleaned_limit = 0
        for p in existing_positions:
            sym = p['symbol']
            mid = sym.replace('/USDT:USDT', 'USDT')
            # 查询该币种上所有条件单
            try:
                algos = b.fapiPrivateGetOpenAlgoOrders({'symbol': mid})
                if len(algos) > 1:
                    # 保留最新的1个，删掉老的
                    sorted_a = sorted(algos, key=lambda x: int(x.get('algoId', 0)), reverse=True)
                    for old in sorted_a[1:]:
                        try:
                            b.fapiPrivateDeleteAlgoOrder({'algoId': old['algoId']})
                            cleaned_algo += 1
                            print('  删除多余止损: {} algo_id={}'.format(mid, old['algoId']))
                        except:
                            pass
            except:
                pass
            # 查询该币种上所有限价单
            try:
                ords = b.fetch_open_orders(sym)
                sell_ords = [o for o in ords if o['side'] == 'sell']
                if len(sell_ords) > 2:
                    # 保留最新的2个，删掉老的
                    sorted_o = sorted(sell_ords, key=lambda o: int(o['id']), reverse=True)
                    for old in sorted_o[2:]:
                        try:
                            b.cancel_order(old['id'], sym)
                            cleaned_limit += 1
                            print('  删除多余止盈: {} price={} id={}'.format(sym, old['price'], old['id']))
                        except:
                            pass
            except:
                pass
        if cleaned_algo == 0 and cleaned_limit == 0:
            print('  无孤儿单需清理')
        else:
            print('  清理完成: 止损{}个 止盈{}个'.format(cleaned_algo, cleaned_limit))
        print()

        # ── 对已有持仓重新评分分析 ──
        print('--- 已有持仓重新评分分析 ---')
        closed_positions, held_positions = review_existing_positions(existing_positions)
        if closed_positions:
            print('主动平仓 {} 个:'.format(len(closed_positions)))
            for cp in closed_positions:
                print('  {} {} ({}: {})'.format(cp['symbol'].ljust(25), cp['side'].ljust(5), cp['reason'], cp['entry']))
        if held_positions:
            print('继续持有 {} 个:'.format(len(held_positions)))
            for s in held_positions:
                print('  {}'.format(s))
        print()

        # 重新获取最新持仓（可能有平仓的）
        existing_positions = get_open_positions()
        existing_symbols_side = {(p['symbol'], p['side']) for p in existing_positions}
        existing_symbols = {p['symbol'] for p in existing_positions}
    
    futures = [(m['id'], m['symbol']) for m in b.markets.values()
               if m.get('swap') and m['settle'] == 'USDT' and m['active']
               and m['id'] not in EXCLUDE]

    total = len(futures)
    opened = []
    failed = []
    skipped_existing = 0

    print('扫描 {} 个合约...'.format(total))
    print()

    for i, (mid, sym) in enumerate(futures):
        result = analyze_symbol(mid, sym)
        if result:
            # 同币种已有持仓则跳过，防止无限叠加
            if result['symbol'] in existing_symbols:
                skipped_existing += 1
                continue
            print('{} {} 评分{} 费{:+.4f}%'.format(
                result['symbol'].ljust(20), result['direction'].ljust(5),
                result['score'], result['fr_val']))

            success = None
            try:
                success = open_position(
                    mid=mid, symbol=result['symbol'],
                    side=result['direction'],
                    entry_price=result['entry_price'],
                    stop_price=result['stop_price'],
                    tp1_price=result['tp1_price'],
                    tp2_price=result['tp2_price'],
                    fr_val=result['fr_val'],
                    reasons=result['reasons'],
                    pos=result.get('pos', None),
                )
            except Exception as e:
                err_msg = str(e)[:60]
                result['fail_reason'] = err_msg
                print('  开仓失败: {}'.format(err_msg))

            if success == 'opened':
                opened.append(result)
                print()
            elif success == 'skipped':
                print('  (跳过: 条件不符合，不计入新开仓)')
            elif success is None:
                failed.append({'symbol': result['symbol'], 'reason': str(e)[:60]})

            time.sleep(1)

        if (i + 1) % 100 == 0:
            sys.stderr.write('进度: {}/{}\n'.format(i + 1, total))

    print('=' * 60)
    print('扫描完成')
    print('总合约: {}'.format(total))

    if opened:
        print('新开仓: {} 个'.format(len(opened)))
        for r in opened:
            print('  {} {} 入场{} 止损{} {}'.format(
                r['symbol'].ljust(20), r['direction'].ljust(5),
                r['entry_price'], r['stop_price'], r['reasons'][:40]))
    else:
        print('新开仓: 0 个')

    if closed_positions:
        print('本轮主动平仓: {} 个'.format(len(closed_positions)))
    if skipped_existing:
        print('跳过已有持仓: {} 个'.format(skipped_existing))
    if failed:
        print('开仓失败: {} 个'.format(len(failed)))
        for f in failed:
            print('  {} {}'.format(f['symbol'].replace('/USDT:USDT',''), f['reason'][:40]))

    print()
    # 直接查询币安实时持仓数量
    actual_positions = get_open_positions()
    print('当前总持仓: {} 个 (新开{}，跳过未开不计数)'.format(len(actual_positions), len(opened)))

    result_data = {
        'timestamp': timestamp,
        'total_scanned': total,
        'new_opened': len(opened),
        'closed_by_review': len(closed_positions),
        'existing_positions': len(existing_positions),
        'skipped_existing': skipped_existing,
        'opened': [{
            'symbol': r['symbol'],
            'direction': r['direction'],
            'score': r['score'],
            'entry_price': r['entry_price'],
            'stop_price': r['stop_price'],
            'fr_val': r['fr_val'],
        } for r in opened],
        'review_closed': [{
            'symbol': cp['symbol'],
            'side': cp['side'],
            'reason': cp['reason'],
            'entry': cp['entry'],
        } for cp in closed_positions],
    }

    result_path = os.path.join(WORKSPACE, 'data', 'latest_trade_result.json')
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    with open(result_path, 'w') as f:
        json.dump(result_data, f, indent=2, default=str)

    # ── Telegram 推送 ──
    msg_lines = ['🤖 山寨异动策略扫描 {}'.format(timestamp)]
    msg_lines.append('扫描 {} 个合约 | 持仓 {} 个'.format(total, len(actual_positions)))
    if opened:
        msg_lines.append('')
        msg_lines.append('📈 新开仓 {} 个:'.format(len(opened)))
        for r in opened:
            sym = r['symbol'].replace('/USDT:USDT', '')
            entry = r['entry_price']
            stop = r['stop_price']
            pct = abs(entry - stop) / entry * 100
            msg_lines.append('  {} {} 入场{:.8f} 止损{:.8f} ({:.0f}%)'.format(sym, r['direction'], entry, stop, pct))
    if closed_positions:
        msg_lines.append('')
        msg_lines.append('❌ 主动平仓 {} 个:'.format(len(closed_positions)))
        for cp in closed_positions:
            sym = cp['symbol'].replace('/USDT:USDT', '')
            msg_lines.append('  {} {} ({})'.format(sym, cp['side'], cp['reason'][:30]))
    if failed:
        msg_lines.append('')
        msg_lines.append('⚠️ 开仓失败 {} 个:'.format(len(failed)))
        for f in failed:
            sym = f['symbol'].replace('/USDT:USDT', '')
            msg_lines.append('  {} ({})'.format(sym, f['reason'][:40]))
    if not opened and not closed_positions and not failed:
        msg_lines.append('  → 无变化')
    msg_lines.append('')
    msg_lines.append('余额: {} USDT | 可用: {} USDT | 持仓: {} 个'.format(
        bal['total'].get('USDT',0), bal['free'].get('USDT',0), len(actual_positions)))
    send_telegram('\n'.join(msg_lines))

    return opened, existing_positions


if __name__ == '__main__':
    main()
