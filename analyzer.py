"""
analyzer.py — 供 GUI 调用的分析函数
=====================================
封装 binance_scalping_analyzer.py 的核心逻辑,
对外暴露 analyze_coin(symbol, balance=1000) 函数。
"""

import sys
import json
import urllib.request
import time
import ssl
from collections import deque
from datetime import datetime, timezone, timedelta

# ─── Whale Score 引擎 ────────────────────────────────────────────────
from whale_score_engine import calc_whale_score, WEIGHTS
# ─── V2.0 衍生品数据层 + Market Regime ──────────────────────────────
from futures_data import build_derivatives_context, detect_market_regime
from strategy import detect_strategy, required_confirm_count, STRATEGY_NONE

# ─── 配置 ────────────────────────────────────────────────────────────────
BINANCE_SPOT = "https://api.binance.com"
BINANCE_FUTURES = "https://fapi.binance.com"
TZ = timezone(timedelta(hours=8))
NO_TRADE_PROB_DIFF = 15
NO_TRADE_SCORE_DIFF = 10
NO_TRADE_CONFIDENCE = 58
CONFLICT_DECAY_STRONG = 12
CONFLICT_DECAY_WEAK = 6
STRUCTURE_NEUTRAL_DECAY = 8
VOLATILITY_DECAY = 5


def _http_get(url: str, timeout: int = 15) -> bytes:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except urllib.error.URLError as e:
            last_err = e
            if attempt < 2:
                time.sleep(0.5)
    raise last_err


def fetch_klines(symbol: str, interval: str, limit: int = 200, closed_only: bool = False, start_time: int = None) -> list[dict]:
    """
    closed_only=True 时剔除尚未收盘的K线（V2.0 要求：核心信号只用已收盘数据）。
    start_time 用于回测分页拉历史（backtest_v2.py）。
    币安 klines 字段: k[0]=open_time, k[6]=close_time(ms)
    """
    sym = symbol.upper()
    params = f"symbol={sym}&interval={interval}&limit={limit}"
    if start_time:
        params += f"&startTime={start_time}"
    fapi_url = f"{BINANCE_FUTURES}/fapi/v1/klines?{params}"
    spot_url = f"{BINANCE_SPOT}/api/v3/klines?{params}"
    raw = None
    for url in (fapi_url, spot_url):
        try:
            raw = json.loads(_http_get(url).decode("utf-8"))
            break
        except Exception:
            continue
    if raw is None:
        raise ConnectionError(f"无法获取 {sym} K线数据")
    now_ms = int(time.time() * 1000)
    klines = []
    for k in raw:
        item = {
            "time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
            "close_time": int(k[6]),
        }
        if closed_only and item["close_time"] > now_ms:
            continue
        klines.append(item)
    return klines


def fetch_ticker(symbol: str) -> dict:
    sym = symbol.upper()
    for url in (
        f"{BINANCE_FUTURES}/fapi/v1/ticker/24hr?symbol={sym}",
        f"{BINANCE_SPOT}/api/v3/ticker/24hr?symbol={sym}",
    ):
        try:
            return json.loads(_http_get(url).decode("utf-8"))
        except Exception:
            continue
    raise ConnectionError(f"无法获取 {sym} 行情数据")


def fetch_funding_rate(symbol: str) -> dict:
    sym = symbol.upper()
    try:
        url = f"{BINANCE_FUTURES}/fapi/v1/premiumIndex?symbol={sym}"
        data = json.loads(_http_get(url).decode("utf-8"))
        fr = float(data.get("lastFundingRate", 0))
        signal = "neutral"
        if fr > 0.0005:
            signal = "long_crowded"
        elif fr < -0.0005:
            signal = "short_crowded"
        return {"funding_rate": fr, "signal": signal}
    except Exception:
        return {"funding_rate": 0, "signal": "neutral"}


def fetch_movers(min_volume: float = 500_000, top_n: int = 20) -> dict:
    """
    获取所有USDT交易对，按24h涨幅排序，返回{涨幅榜, 跌幅榜}。
    min_volume: 最低成交额过滤(USDT)
    """
    try:
        data = json.loads(_http_get("https://api.binance.com/api/v3/ticker/24hr").decode("utf-8"))
        usdt = [t for t in data if t["symbol"].endswith("USDT")
                 and float(t["quoteVolume"]) > min_volume]
        usdt.sort(key=lambda t: float(t["priceChangePercent"]), reverse=True)

        gainers = []
        for t in usdt[:top_n]:
            gainers.append({
                "symbol": t["symbol"],
                "price": float(t["lastPrice"]),
                "change_pct": round(float(t["priceChangePercent"]), 2),
                "volume": round(float(t["quoteVolume"]) / 1e6, 2),
                "high": float(t["highPrice"]),
                "low": float(t["lowPrice"]),
            })

        losers_all = usdt[-top_n:]
        losers_all.reverse()
        losers = []
        for t in losers_all:
            losers.append({
                "symbol": t["symbol"],
                "price": float(t["lastPrice"]),
                "change_pct": round(float(t["priceChangePercent"]), 2),
                "volume": round(float(t["quoteVolume"]) / 1e6, 2),
                "high": float(t["highPrice"]),
                "low": float(t["lowPrice"]),
            })

        return {"gainers": gainers, "losers": losers, "error": None}
    except Exception as e:
        return {"gainers": [], "losers": [], "error": str(e)}


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return values[-1]
    multiplier = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    for v in values[period:]:
        result = (v - result) * multiplier + result
    return result


def ema_series(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [values[-1]] * len(values)
    multiplier = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    series = [result]
    for v in values[period:]:
        result = (v - result) * multiplier + result
        series.append(result)
    padding = [series[0]] * (period - 1)
    return padding + series


def sma(values: list[float], period: int) -> float:
    if len(values) < period:
        return sum(values) / len(values)
    return sum(values[-period:]) / period


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values: list[float]) -> dict:
    if len(values) < 35:
        return {"macd": 0, "signal": 0, "histogram": 0, "hist_trend": "neutral"}
    ema12 = ema_series(values, 12)
    ema26 = ema_series(values, 26)
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    signal_line = ema_series(macd_line, 9)
    histogram = macd_line[-1] - signal_line[-1]
    hist_series = [macd_line[i] - signal_line[i] for i in range(-3, 0)] if len(macd_line) >= 3 else [0]
    if len(hist_series) >= 3:
        h1, h2, h3 = hist_series
        hist_trend = "rising" if h3 > h2 > h1 else ("falling" if h3 < h2 < h1 else "neutral")
    else:
        hist_trend = "neutral"
    return {"macd": macd_line[-1], "signal": signal_line[-1], "histogram": histogram, "hist_trend": hist_trend, "macd_line_series": macd_line[-5:]}


def find_support_resistance(klines: list[dict], swing_highs: list, swing_lows: list) -> dict:
    price = klines[-1]["close"]
    resistances = sorted(set(h[1] for h in swing_highs), reverse=True)
    supports = sorted(set(l[1] for l in swing_lows))
    nearest_resistance = None
    for r in resistances:
        if r > price:
            nearest_resistance = r
            break
    nearest_support = None
    for s in supports:
        if s < price:
            nearest_support = s
            break
    return {"support": nearest_support, "resistance": nearest_resistance,
            "sup_dist_pct": round((price - nearest_support) / price * 100, 2) if nearest_support else None,
            "res_dist_pct": round((nearest_resistance - price) / price * 100, 2) if nearest_resistance else None}


def atr(klines: list[dict], period: int = 14) -> float:
    if len(klines) < period + 1:
        return (klines[-1]["high"] - klines[-1]["low"]) * 0.01
    tr_values = []
    for i in range(1, len(klines)):
        high, low = klines[i]["high"], klines[i]["low"]
        prev_close = klines[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)
    return sma(tr_values, period)


def find_swing_points(klines: list[dict], window: int = 5) -> dict:
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    swing_highs, swing_lows = [], []
    for i in range(window, len(klines) - window):
        left = highs[i - window:i]
        right = highs[i + 1:i + window + 1]
        if highs[i] > max(left) and highs[i] > max(right):
            swing_highs.append((i, highs[i]))
        left = lows[i - window:i]
        right = lows[i + 1:i + window + 1]
        if lows[i] < min(left) and lows[i] < min(right):
            swing_lows.append((i, lows[i]))
    return {"swing_highs": swing_highs, "swing_lows": swing_lows}


def analyze_structure(klines: list[dict], swing_highs: list, swing_lows: list) -> dict:
    if len(swing_highs) < 3 or len(swing_lows) < 3:
        return {"trend": "neutral", "detail": "摆动点不足"}
    recent_highs = [h[1] for h in swing_highs[-3:]]
    recent_lows = [l[1] for l in swing_lows[-3:]]
    hh = recent_highs[-1] > recent_highs[-2] > recent_highs[-3]
    hl = recent_lows[-1] > recent_lows[-2] > recent_lows[-3]
    lh = recent_highs[-1] < recent_highs[-2] < recent_highs[-3]
    ll = recent_lows[-1] < recent_lows[-2] < recent_lows[-3]
    if hh and hl:
        trend = "bullish"
    elif lh and ll:
        trend = "bearish"
    elif hh and not hl:
        trend = "bearish_div"
    elif ll and not lh:
        trend = "bullish_div"
    else:
        trend = "neutral"
    return {"trend": trend, "recent_highs": recent_highs, "recent_lows": recent_lows,
            "detail": {"higher_high": hh, "higher_low": hl, "lower_high": lh, "lower_low": ll}}


def fibonacci_levels(high: float, low: float) -> dict:
    diff = high - low
    return {"0.000": high, "0.236": high - diff * 0.236, "0.382": high - diff * 0.382,
            "0.500": high - diff * 0.500, "0.618": high - diff * 0.618,
            "0.786": high - diff * 0.786, "1.000": low}


def find_nearest_fib(current_price: float, fib_levels: dict, tolerance_pct: float = 0.003) -> dict:
    result = {"nearest": None, "distance_pct": None, "touch": False}
    for name, level in fib_levels.items():
        dist = abs(current_price - level) / current_price
        if result["nearest"] is None or dist < result["distance_pct"]:
            result["nearest"] = name
            result["distance_pct"] = dist
            result["touch"] = dist < tolerance_pct
    return result


def detect_candlestick_patterns(klines: list[dict]) -> dict:
    if len(klines) < 3:
        return {}
    c1, c2, c3 = klines[-3], klines[-2], klines[-1]
    patterns = {}
    bullish_engulf = (c2["close"] > c2["open"] and c1["close"] < c1["open"] and c2["close"] > c1["open"] and c2["open"] < c1["close"])
    patterns["bullish_engulfing"] = bullish_engulf
    bearish_engulf = (c2["close"] < c2["open"] and c1["close"] > c1["open"] and c2["close"] < c1["open"] and c2["open"] > c1["close"])
    patterns["bearish_engulfing"] = bearish_engulf
    for idx, c in [("c2", c2), ("c3", c3)]:
        body = abs(c["close"] - c["open"])
        upper_wick = c["high"] - max(c["open"], c["close"])
        lower_wick = min(c["open"], c["close"]) - c["low"]
        total = c["high"] - c["low"]
        if total == 0:
            continue
        if lower_wick > body * 2 and upper_wick < body * 0.5:
            patterns[f"pin_bar_bullish_{idx}"] = True
        if upper_wick > body * 2 and lower_wick < body * 0.5:
            patterns[f"pin_bar_bearish_{idx}"] = True
    for idx, c in [("c2", c2), ("c3", c3)]:
        body = abs(c["close"] - c["open"])
        total = c["high"] - c["low"]
        if total > 0 and body / total < 0.1:
            patterns[f"doji_{idx}"] = True
    prev_low = min(c1["low"], c2["low"])
    prev_high = max(c1["high"], c2["high"])
    if c3["low"] < prev_low and c3["close"] > prev_low:
        patterns["fake_break_below"] = True
    if c3["high"] > prev_high and c3["close"] < prev_high:
        patterns["fake_break_above"] = True
    return patterns


def analyze_volume(klines: list[dict]) -> dict:
    volumes = [k["volume"] for k in klines]
    avg_vol = sma(volumes, 20)
    current_vol = volumes[-1]
    ratio = current_vol / avg_vol if avg_vol > 0 else 1.0
    signal = "normal"
    if ratio > 3:
        signal = "volume_spike"
    elif ratio < 0.5:
        signal = "volume_shrink"
    return {"ratio": round(ratio, 1), "signal": signal}


def score_trend(klines_15m: list[dict], klines_5m: list[dict], klines_1m: list[dict]) -> dict:
    closes_15m = [k["close"] for k in klines_15m]
    closes_5m = [k["close"] for k in klines_5m]
    ema20_15m = ema(closes_15m, 20)
    ema50_15m = ema(closes_15m, 50)
    ema200_15m = ema(closes_15m, 200)
    price_15m = closes_15m[-1]
    trend_15m = "bull" if ema20_15m > ema50_15m else "bear"
    price_above_ema20_15m = price_15m > ema20_15m
    ema20_5m = ema(closes_5m, 20)
    ema50_5m = ema(closes_5m, 50)
    price_5m = closes_5m[-1]
    trend_5m = "bull" if ema20_5m > ema50_5m else "bear"
    closes_1m = [k["close"] for k in klines_1m]
    # V2.1: 1m 数据可能缺失（60天纯技术面回测只拉最近2天），降级用5m趋势
    if len(closes_1m) >= 20:
        ema20_1m = ema(closes_1m, 20)
        ema50_1m = ema(closes_1m, 50)
        price_1m = closes_1m[-1]
        trend_1m = "bull" if ema20_1m > ema50_1m else "bear"
    else:
        trend_1m = trend_5m  # 降级
    tf_bullish = sum(1 for t in [trend_15m, trend_5m, trend_1m] if t == "bull")
    tf_aligned = tf_bullish == 3 or tf_bullish == 0
    tf_dominant = "bull" if tf_bullish >= 2 else "bear"
    return {"trend_15m": trend_15m, "trend_5m": trend_5m, "trend_1m": trend_1m,
            "ema20": ema20_15m, "ema50": ema50_15m, "ema200": ema200_15m,
            "price_above_ema20": price_above_ema20_15m,
            "ema20_5m": ema20_5m, "ema50_5m": ema50_5m,
            "tf_aligned": tf_aligned, "tf_dominant": tf_dominant}


def score_system(price: float, klines_15m: list[dict], klines_5m: list[dict], klines_1m: list[dict], ticker: dict, funding: dict, symbol: str = "", derivatives: dict = None, regime: dict = None, btc_regime: dict = None, whale_override: dict = None) -> dict:
    """
    V2.1 信号引擎核心（《V2.0代码整改与V2.1策略研究实施说明》第2/3/5/7/8/18/19/21/22节）。

    流程：Market Regime → Strategy → Candidate Direction → 必要条件 → 确认条件 → READY/WAIT

    语义（V2.1 修复）：
      - signal_status : NO_TRADE(无策略假设) / WAIT(条件未满足) / READY(可交易)
      - candidate_direction : 策略假设的方向（观察状态，可保留方向但不交易）
      - trade_direction     : 仅 READY 时有 LONG/SHORT，否则 NEUTRAL
      - long_probability / short_probability 是评分映射的 score_implied 伪概率，
        不是统计胜率（文档第5节），禁止解释为"历史盈利概率"
      - WAIT 是观察状态，不是失败交易（文档第4节）
      - RSI/MACD/EMA 降级为 Context，不再直接加减分（文档第20节）
      - OI/Funding 表达市场状态，不再机械当作方向票（文档第16/17节）
      - Whale 评分退出交易决策，仅供显示（文档原则6）
    """
    closes_15m = [k["close"] for k in klines_15m]
    closes_5m = [k["close"] for k in klines_5m]
    closes_1m = [k["close"] for k in klines_1m]

    # V2.0: Market Regime / 衍生品上下文（缺省时保守回退）
    if regime is None:
        regime = {"regime": "RANGE", "regime_name": "RANGE", "trend_4h": "FLAT", "trend_1h": "FLAT",
                  "high_volatility": False, "reasons": []}
    if derivatives is None:
        derivatives = {}
    regime_name = regime.get("regime_name") or regime.get("regime", "RANGE")
    btc_regime_name = (btc_regime or {}).get("regime_name") or (btc_regime or {}).get("regime", "RANGE")

    long_score = 0
    short_score = 0
    warnings = []
    reasons_long = []
    reasons_short = []

    # ── 指标计算（全部基于已收盘 K 线；price 为实时触发价）──
    rsi_15m = rsi(closes_15m, 14)
    macd_data = macd(closes_15m)
    atr_val = atr(klines_15m, 14)
    atr_pct = (atr_val / price * 100) if price > 0 else 0

    # ── 结构 ──
    swing = find_swing_points(klines_15m, 5)
    sr = find_support_resistance(klines_15m, swing["swing_highs"], swing["swing_lows"])
    struct = analyze_structure(klines_15m, swing["swing_highs"], swing["swing_lows"])
    struct_trend = struct["trend"]
    fib = fibonacci_levels(max(k["high"] for k in klines_15m[-48:]), min(k["low"] for k in klines_15m[-48:]))
    fib_near = find_nearest_fib(price, fib)

    # ── 成交量 / K线形态 ──
    vol_analysis = analyze_volume(klines_15m)
    vol_ratio = vol_analysis["ratio"]
    patterns_15m = detect_candlestick_patterns(klines_15m)

    # ── 多周期趋势（仅作 Context，文档20节）──
    trend_info = score_trend(klines_15m, klines_5m, klines_1m)
    ema20_val = trend_info["ema20"]
    ema50_val = trend_info["ema50"]
    ema200_val = trend_info["ema200"]
    ema_bull = ema20_val > ema50_val > ema200_val
    ema_bear = ema20_val < ema50_val < ema200_val

    # ── 24h 行情 ──
    change_24h = float(ticker.get("priceChangePercent", 0))
    high_24h = float(ticker.get("highPrice", 0))
    low_24h = float(ticker.get("lowPrice", 0))
    range_24h = high_24h - low_24h
    range_pct = ((price - low_24h) / range_24h * 100) if range_24h > 0 else 50.0

    # ── RSI/MACD/EMA 降级为 Context（文档20节）──
    rsi_context = "neutral"
    if rsi_15m < 30:
        rsi_context = "oversold"
    elif rsi_15m > 70:
        rsi_context = "overbought"
    elif rsi_15m < 40:
        rsi_context = "weak"
    elif rsi_15m > 60:
        rsi_context = "strong"
    if rsi_context in ("oversold", "overbought"):
        warnings.append(f"RSI{rsi_context} ({rsi_15m:.1f})，属环境context，需结构/确认条件配合")
    macd_context = macd_data["hist_trend"]  # rising / falling / neutral
    ema_context = "bull" if ema_bull else ("bear" if ema_bear else "mixed")

    # ── 方向倾向评分（仅技术位置因子；非概率，仅供展示与 grade）──
    if struct_trend == "bullish":
        long_score += 10
        reasons_long.append("上涨结构 (HH+HL)")
    elif struct_trend == "bearish":
        short_score += 10
        reasons_short.append("下跌结构 (LH+LL)")
    elif struct_trend == "bearish_div":
        long_score += 4
        reasons_long.append("底背离可能")
    elif struct_trend == "bullish_div":
        short_score += 4
        reasons_short.append("顶背离可能")

    if fib_near["nearest"] and fib_near["touch"]:
        if price < ema20_val:
            short_score += 6
            reasons_short.append(f"回抽Fib {fib_near['nearest']} 阻力位")
        else:
            long_score += 6
            reasons_long.append(f"回踩Fib {fib_near['nearest']} 支撑位")

    if patterns_15m.get("bullish_engulfing") or patterns_15m.get("pin_bar_bullish_c3"):
        long_score += 6
        reasons_long.append("下影Pin Bar (看涨)")
    if patterns_15m.get("bearish_engulfing") or patterns_15m.get("pin_bar_bearish_c3"):
        short_score += 6
        reasons_short.append("上影Pin Bar (看跌)")
    if patterns_15m.get("fake_break_below"):
        long_score += 6
        reasons_long.append("假跌破支撑后收回")
    if patterns_15m.get("fake_break_above"):
        short_score += 6
        reasons_short.append("假突破阻力后回落")

    if range_pct <= 30:
        if struct_trend in ("bullish", "neutral"):
            long_score += 12
            reasons_long.append(f"价格在24h区间低位 ({range_pct:.0f}%)")
        else:
            short_score += 8
            reasons_short.append(f"区间低位({range_pct:.0f}%, 趋势偏空) 可能是下跌中继")
    elif range_pct > 85:
        if struct_trend in ("bearish", "neutral"):
            short_score += 15
            reasons_short.append(f"价格在24h区间极高位 ({range_pct:.0f}%)")
        else:
            long_score -= 12
            warnings.append(f"区间极高位({range_pct:.0f}%), 等回调再入场")
    elif range_pct > 70:
        if struct_trend in ("bearish", "neutral"):
            short_score += 10
            reasons_short.append(f"价格在24h区间高位 ({range_pct:.0f}%)")
        else:
            long_score -= 6
            short_score += 4
            reasons_short.append(f"24h区间高位 ({range_pct:.0f}%), 入场偏后")

    s = sr.get("support")
    r = sr.get("resistance")
    sd = sr.get("sup_dist_pct")
    rd = sr.get("res_dist_pct")
    rr_long = rr_short = None
    if s is not None and r is not None and sd is not None and rd is not None and sd > 0 and rd > 0:
        rr_long = rd / sd
        rr_short = sd / rd
        if rr_long >= 2.0:
            long_score += 8
            reasons_long.append(f"入场盈亏比有利 (1:{rr_long:.1f})")
        elif rr_long < 1.0:
            short_score += 6
            reasons_short.append(f"做多盈亏比差 (1:{rr_long:.1f})")
        if rr_short >= 2.0:
            short_score += 8
            reasons_short.append(f"做空盈亏比有利 (1:{rr_short:.1f})")
        elif rr_short < 1.0:
            long_score += 6
            reasons_long.append(f"做空盈亏比差 (1:{rr_short:.1f})")
    elif s is not None and sd is not None and sd < 1.5:
        long_score += 6
        reasons_long.append(f"支撑位很近 (-{sd:.1f}%)")
    elif r is not None and rd is not None and rd < 1.5:
        short_score += 6
        reasons_short.append(f"阻力位很近 (+{rd:.1f}%)")

    if vol_analysis["signal"] == "volume_spike":
        if patterns_15m.get("bullish_engulfing") or patterns_15m.get("pin_bar_bullish_c3"):
            long_score += 8
            reasons_long.append(f"放量配合 (x{vol_ratio:.1f})")
        elif patterns_15m.get("bearish_engulfing") or patterns_15m.get("pin_bar_bearish_c3"):
            short_score += 8
            reasons_short.append(f"放量配合 (x{vol_ratio:.1f})")
        else:
            warnings.append(f"异常放量 (x{vol_ratio:.1f})，注意变盘")

    if atr_pct > 5:
        warnings.append(f"高波动 (ATR={atr_pct:.1f}%)，止损需放宽")
    elif atr_pct < 1:
        warnings.append(f"低波动 (ATR={atr_pct:.1f}%)，注意突破")

    # ── 衍生品因子 = 市场状态 / 确认条件（文档16/17节，不再机械加减分）──
    oi_trend = derivatives.get("oi_trend", "unknown")
    oi_chg_15m = derivatives.get("oi_change_15m")
    oi_participation = oi_chg_15m is not None and abs(oi_chg_15m) >= 1.0
    taker_trend = derivatives.get("taker_trend", "neutral")
    funding_regime = derivatives.get("funding_regime", "normal")
    funding_trend = derivatives.get("funding_trend", "flat")
    funding_acc = derivatives.get("funding_acceleration", 0.0)
    funding_rate_now = derivatives.get("funding_rate")
    liq_5m_long = derivatives.get("liq_5m_long")
    liq_5m_short = derivatives.get("liq_5m_short")

    # ── V2.1 Strategy 层（文档21节）──
    strat_ctx = {
        "struct_trend": struct_trend,
        "patterns": patterns_15m,
        "sup_dist_pct": sd,
        "res_dist_pct": rd,
        "fib_nearest": fib_near["nearest"],
        "fib_touch": fib_near["touch"],
        "price": price,
        "ema20": ema20_val,
        "high_volatility": regime.get("high_volatility", False),
        "range_pct": range_pct,
    }
    strat = detect_strategy(regime_name, strat_ctx)
    strategy = strat["strategy"]
    setup = strat["setup"]
    candidate_direction = strat["candidate_direction"]
    counter_trend = strat["counter_trend"]
    reason_codes = list(strat["reason_codes"])

    # ── V2.1 必要条件（文档8.1节：全部必须满足）──
    sup_ref = sd if sd is not None else ((price - low_24h) / price * 100 if low_24h > 0 and price > low_24h else None)
    res_ref = rd if rd is not None else ((high_24h - price) / price * 100 if high_24h > price else None)
    needs = {"setup_valid": False, "entry_zone": False, "structure_5m": False, "rr_pass": False}
    if strategy != STRATEGY_NONE and candidate_direction in ("LONG", "SHORT"):
        needs["setup_valid"] = True
        if candidate_direction == "LONG":
            needs["entry_zone"] = (sup_ref is not None and sup_ref <= 2.0) or (fib_near["nearest"] and fib_near["touch"] and price <= ema20_val)
            needs["structure_5m"] = trend_info["trend_5m"] == "bull"
            rr_cur = rr_long
        else:
            needs["entry_zone"] = (res_ref is not None and res_ref <= 2.0) or (fib_near["nearest"] and fib_near["touch"] and price >= ema20_val)
            needs["structure_5m"] = trend_info["trend_5m"] == "bear"
            rr_cur = rr_short
        needs["rr_pass"] = rr_cur is not None and rr_cur >= 1.2  # 最低盈亏比要求（文档8.1）

    # ── V2.1 确认条件（文档8.2节：确认因素，非独立计票；缺数据跳过）──
    confirms = []
    if candidate_direction == "LONG":
        vol_ok = vol_ratio >= 1.2 or (vol_analysis["signal"] == "volume_spike" and (patterns_15m.get("bullish_engulfing") or patterns_15m.get("pin_bar_bullish_c3")))
        if vol_ok:
            confirms.append("VOLUME_CONFIRM")
        if oi_participation:
            confirms.append("OI_PARTICIPATION")
        if taker_trend == "buy_dominant":
            confirms.append("TAKER_SUPPORT")
        if funding_regime in ("normal", "short_crowded", "extreme_short"):
            confirms.append("FUNDING_OK")
    elif candidate_direction == "SHORT":
        vol_ok = vol_ratio >= 1.2 or (vol_analysis["signal"] == "volume_spike" and (patterns_15m.get("bearish_engulfing") or patterns_15m.get("pin_bar_bearish_c3")))
        if vol_ok:
            confirms.append("VOLUME_CONFIRM")
        if oi_participation:
            confirms.append("OI_PARTICIPATION")
        if taker_trend == "sell_dominant":
            confirms.append("TAKER_SUPPORT")
        if funding_regime in ("normal", "long_crowded", "extreme_long"):
            confirms.append("FUNDING_OK")

    # 确认门槛：顺势2 / 逆势+1 / 高波动+1（文档8.2/19节）
    need_confirm = required_confirm_count(counter_trend, regime.get("high_volatility", False))

    # ── V2.1 交易域过滤（文档32节：方向独立验证；域外候选 → NO_TRADE）──
    from strategy import domain_allows
    domain_ok, domain_reason = domain_allows(regime_name, strategy, candidate_direction)

    # ── V2.1 状态机判定（文档25节）──
    if not domain_ok:
        signal_status = "NO_TRADE"
        no_trade = True
        no_trade_reason = domain_reason
    elif strategy == STRATEGY_NONE:
        signal_status = "NO_TRADE"
        no_trade = True
        no_trade_reason = strat.get("block_reason") or "无匹配策略/位置条件"
    elif not all(needs.values()):
        signal_status = "WAIT"
        no_trade = False
        no_trade_reason = ""
    elif len(confirms) < need_confirm:
        signal_status = "WAIT"
        no_trade = False
        no_trade_reason = ""
    else:
        signal_status = "READY"
        no_trade = False
        no_trade_reason = ""

    # P0-1/P0-3: trade_direction 仅 READY 才有方向
    trade_direction = candidate_direction if signal_status == "READY" else "NEUTRAL"

    missing = [k for k, ok in needs.items() if not ok]
    trigger_met = [f"必要:{k}" for k, ok in needs.items() if ok] + [f"确认:{c}" for c in confirms]
    trigger_missing = [f"必要:{k}" for k in missing]
    if not missing and len(confirms) < need_confirm:
        trigger_missing.append(f"确认不足({len(confirms)}/{need_confirm})")
    if signal_status == "READY":
        for k, ok in needs.items():
            if ok:
                reason_codes.append(f"NEED_{k.upper()}_OK")
        reason_codes += [f"CONFIRM_{c}" for c in confirms]

    # 最近结构低点/高点（V2.0 结构止损依据）
    swing_low_recent = None
    swing_high_recent = None
    for sl_ in reversed(swing.get("swing_lows", [])):
        if sl_[1] < price:
            swing_low_recent = sl_[1]
            break
    for sh_ in reversed(swing.get("swing_highs", [])):
        if sh_[1] > price:
            swing_high_recent = sh_[1]
            break

    # ── 伪概率（score-implied，非统计概率；文档5节）──
    max_possible = 100
    net_score = long_score - short_score
    long_prob_raw = 50.0 + (net_score / max_possible) * 50.0
    long_prob = round(max(5, min(95, long_prob_raw)), 1)
    short_prob = round(100 - long_prob, 1)
    score_confidence = max(long_prob, short_prob)

    # 信号等级（仅表示评分强度，不代表交易建议）
    signal_strength = score_confidence
    dominant_score = max(long_score, short_score)
    if signal_strength >= 72 and trend_info["tf_aligned"] and dominant_score >= 30:
        grade = "A"
    elif signal_strength >= 65 and dominant_score >= 20:
        grade = "B"
    elif signal_strength >= 60:
        grade = "C"
    else:
        grade = "D"
    if no_trade:
        grade = "N"

    long_str = candidate_direction if candidate_direction in ("LONG", "SHORT") else ("LONG" if long_prob >= 50 else "SHORT")

    return {
        "long_probability": long_prob,
        "short_probability": short_prob,
        "probability_note": "score_implied，非统计胜率（V2.1）",
        "long_score": round(long_score),
        "short_score": round(short_score),
        "direction_score": round(net_score),
        "score_confidence": round(score_confidence, 1),
        "grade": grade,
        "direction_hint": long_str,
        "rsi": round(rsi_15m, 1),
        "ema20": round(ema20_val, 4),
        "ema50": round(ema50_val, 4),
        "atr_pct": round(atr_pct, 2),
        "macd_trend": macd_data["hist_trend"],
        "structure": struct_trend,
        "volume_ratio": vol_ratio,
        "funding_rate": funding["funding_rate"],
        "fib_nearest": fib_near["nearest"],
        "range_percentile": round(range_pct, 1),
        "rr_metric": round(rr_long, 1) if rr_long is not None else "-",
        "support": sr["support"],
        "resistance": sr["resistance"],
        "sup_dist_pct": sr["sup_dist_pct"],
        "res_dist_pct": sr["res_dist_pct"],
        "high_24h": high_24h,
        "low_24h": low_24h,
        "reasons_long": reasons_long[:6],
        "reasons_short": reasons_short[:6],
        "warnings": warnings[:4],
        "tf_aligned": trend_info["tf_aligned"],
        "change_24h": round(change_24h, 2),
        "whale_score": 50.0,
        "whale_grade_label": "中性 ➖",
        "whale_factors": {},
        # ── V2.0 兼容 ──
        "market_regime": regime_name,
        "trend_4h": regime.get("trend_4h", "FLAT"),
        "trend_1h": regime.get("trend_1h", "FLAT"),
        "regime_reasons": regime.get("reasons", []),
        "btc_regime": btc_regime_name,
        "no_trade": no_trade,
        "no_trade_reason": no_trade_reason,
        "entry_trigger": signal_status,
        "signal_status": signal_status,
        "trigger_met": trigger_met,
        "trigger_missing": trigger_missing,
        "swing_low_recent": swing_low_recent,
        "swing_high_recent": swing_high_recent,
        "atr_val": round(atr_val, 6),
        "oi": derivatives.get("oi"),
        "oi_change_5m": derivatives.get("oi_change_5m"),
        "oi_change_15m": derivatives.get("oi_change_15m"),
        "oi_change_1h": derivatives.get("oi_change_1h"),
        "oi_trend": oi_trend,
        "funding_trend": funding_trend,
        "funding_regime": funding_regime,
        "taker_ratio": derivatives.get("taker_ratio"),
        "taker_trend": taker_trend,
        "global_ls_ratio": derivatives.get("global_ls_ratio"),
        "liq_5m_long": liq_5m_long,
        "liq_5m_short": liq_5m_short,
        # ── V2.1 新增 ──
        "strategy": strategy,
        "setup": setup,
        "candidate_direction": candidate_direction,
        "trade_direction": trade_direction,
        "counter_trend": counter_trend,
        "reason_codes": reason_codes,
        "context": {
            "rsi": rsi_context,
            "macd_hist": macd_context,
            "ema": ema_context,
            "vol_ratio": round(vol_ratio, 1),
            "change_24h": round(change_24h, 2),
            "funding_acceleration": funding_acc,
        },
        "regime_strength": regime.get("regime_strength", 0.0),
        "volatility_state": regime.get("volatility_state", "UNKNOWN"),
        "need_confirm": need_confirm,
        "confirm_count": len(confirms),
    }


def format_gui_details(d: dict) -> str:
    """
    GUI 多空逻辑详细文本（后端生成，前端直接 setText）
    接收 analyze_coin_dict 返回的 dict。
    """
    parts = []

    # ── V2.0 市场环境 ──
    regime = d.get("market_regime", "—")
    trend_4h = d.get("trend_4h", "—")
    trend_1h = d.get("trend_1h", "—")
    btc_regime = d.get("btc_regime", "—")
    parts.append(f"🌍 市场环境: {regime}  (4H:{trend_4h} / 1H:{trend_1h} | BTC:{btc_regime})")
    # ── V2.1 策略层 ──
    strat = d.get("strategy", "NONE")
    setup = d.get("setup", "NONE")
    cand = d.get("candidate_direction", "NEUTRAL")
    if strat != "NONE":
        ct_tag = "⚠️逆势" if d.get("counter_trend") else "顺势"
        parts.append(f"   🎯 策略: {strat} ({setup})  候选方向: {cand} [{ct_tag}]")
    else:
        parts.append(f"   🎯 策略: 无匹配假设（不交易）")
    oi_chg = d.get("oi_change_15m")
    if oi_chg is not None:
        parts.append(f"   OI 15m: {oi_chg:+.1f}% ({d.get('oi_trend','—')})  |  Taker: {d.get('taker_trend','—')}  |  Funding: {d.get('funding_regime','—')} 趋势{d.get('funding_trend','—')}")
    nt = d.get("no_trade_reason")
    if nt:
        parts.append(f"🚫 {nt}")
    parts.append("")

    # ── V2.0 信号状态 ──
    st = d.get("signal_status", "WAIT")
    if st == "READY":
        parts.append("📡 信号状态: ✅ READY")
    elif st == "NO_TRADE":
        parts.append("📡 信号状态: 🚫 NO TRADE")
    else:
        parts.append("📡 信号状态: ⏸ WAIT")
    if st == "WAIT" and d.get("trigger_missing"):
        parts.append("   等待: " + ", ".join(d["trigger_missing"][:4]))
    elif st == "READY" and d.get("trigger_met"):
        parts.append("   已满足: " + ", ".join(d["trigger_met"]))
    parts.append("")

    ema20 = d.get("ema20", 0)
    ema50 = d.get("ema50", 0)
    rsi_val = d.get("rsi", "—")
    parts.append(f"📈 技术指标")
    parts.append(f"   RSI(14): {rsi_val}  |  EMA20: ${ema20:,.4f}  |  EMA50: ${ema50:,.4f}")
    parts.append(f"   结构: {d.get('structure','—')}  |  ATR: {d.get('atr_pct',0):.2f}%  |  MACD: {d.get('macd_trend','—').capitalize()}")
    funding = d.get('funding_rate', 0)
    aligned = d.get('tf_aligned', False)
    parts.append(f"   量比: x{d.get('volume_ratio',0):.1f}  |  资金费率: {funding:+.6f}  |  共振: {'✅' if aligned else '❌'}")

    fib = d.get('fib_nearest')
    if fib:
        parts.append(f"   Fib靠近: {fib}")

    rp = d.get('range_percentile')
    rm = d.get('rr_metric', '-')
    if rp is not None:
        parts.append(f"   区间百分位: {rp:.0f}%  |  S/R盈亏比: {rm}")

    support = d.get('support')
    resistance = d.get('resistance')
    if support and resistance:
        sup_d = d.get('sup_dist_pct', 0)
        res_d = d.get('res_dist_pct', 0)
        parts.append(f"   S/R: 支撑 ${support:,.4f} (-{sup_d}%)  |  阻力 ${resistance:,.4f} (+{res_d}%)")

    ws = d.get("whale_score", 50)
    wl = d.get("whale_grade_label", "中性 ➖")
    ws_emoji = "🔴" if ws < 40 else ("🟢" if ws > 60 else "⚪")
    parts.append(f"   🐋 巨鲸评分: {ws:.0f}/100 {wl}")
    factors = d.get("whale_factors", {})
    factor_map = {
        "exchange": ("🏦 Taker资金流", "20%"),
        "holding": ("🐳 持仓变化", "20%"),
        "transfer": ("🔄 大额转账", "15%"),
        "concentration": ("🎯 集中度", "5%"),
        "smart_money": ("🧠 聪明钱", "30%"),
        "orderbook": ("📚 订单簿", "10%"),
    }
    for fkey, (flabel, fweight) in factor_map.items():
        f = factors.get(fkey, {})
        fs = f.get("score", 50)
        fd = f.get("detail", "")
        f_emoji = "🔴" if fs < 40 else ("🟢" if fs > 60 else "⚪")
        parts.append(f"     {f_emoji} {flabel} {fs}分 ({fweight}) {fd}")

    parts.append("")

    long_score = d.get("long_score", 0)
    long_reasons = d.get("reasons_long", [])
    parts.append(f"🟢 做多理由 ({long_score}分)")
    for r in long_reasons:
        parts.append(f"  ✓ {r}")
    parts.append("")

    short_score = d.get("short_score", 0)
    short_reasons = d.get("reasons_short", [])
    parts.append(f"🔴 做空理由 ({short_score}分)")
    for r in short_reasons:
        parts.append(f"  ✓ {r}")

    for w in d.get("warnings", []):
        parts.append(f"\n⚡ {w}")

    return "\n".join(parts if len(parts) > 2 else ["💡 正在计算指标多空强弱逻辑..."])


def risk_recommendation(price: float, score_result: dict, account_balance: float = 1000.0) -> dict:
    """
    V2.1: 仅 READY 信号生成交易参数（文档第3/4节）。

    - WAIT / NO_TRADE → direction=NEUTRAL，entry/SL/TP 全 None，position_size=0
    - READY → 结构止损 + 动态 TP（保留 V2.0 已验证组件）
    - Whale Score 不再参与仓位/杠杆修正（文档原则6）
    """
    neutral = {
        "direction": "NEUTRAL",
        "confidence": score_result.get("score_confidence", 0),
        "entry_price": None,
        "stop_loss": None,
        "take_profit": None,
        "sl_pct": None,
        "tp_pct": None,
        "rr_ratio": None,
        "tp2_price": None,
        "sl_basis": None,
        "leverage": 0,
        "position_pct": 0,
        "notional_value": 0,
        "risk_amount": 0,
    }

    # P0-1: WAIT 不得产生交易方向 / 参数
    if score_result.get("signal_status") != "READY":
        return neutral
    direction = score_result.get("trade_direction", "NEUTRAL")
    if direction not in ("LONG", "SHORT"):
        return neutral

    atr_pct = score_result["atr_pct"]
    score_confidence = score_result.get("score_confidence") or max(
        score_result.get("long_probability", 50), score_result.get("short_probability", 50))

    # ── V2.0 结构止损 + 动态 TP（文档27节）──
    # SL = 结构位(留0.1%缓冲) 与 ATR 保护组合：至少 ATR×1，至多 ATR×3
    # TP1 = 最近结构阻力/支撑，TP2 = 24h 极值（下一流动性目标）
    if score_confidence >= 75:
        conf_mult = 0.9
    elif score_confidence >= 65:
        conf_mult = 1.0
    else:
        conf_mult = 1.1
    atr_val = score_result.get("atr_val") or price * atr_pct / 100

    sl_basis = "ATR"
    if direction == "LONG":
        swing_ref = score_result.get("swing_low_recent")
        if swing_ref and swing_ref < price:
            struct_dist = (price - swing_ref * 0.999) / price * 100
            sl_pct = max(struct_dist, atr_pct * 1.0)
            sl_basis = "结构低点"
        else:
            sl_pct = atr_pct * 1.5
        sl_pct = min(sl_pct, atr_pct * 3.0)
        sl_pct = max(sl_pct, 0.5) * conf_mult
        stop_loss = price * (1 - sl_pct / 100)
        tp1_price = score_result.get("resistance")
        if tp1_price and tp1_price > price:
            tp1_pct = (tp1_price - price) / price * 100
        else:
            tp1_pct = sl_pct * 2
        tp2_price = score_result.get("high_24h")
        if tp2_price and tp2_price > price * (1 + tp1_pct / 100) * 1.001:
            tp2_pct = (tp2_price - price) / price * 100
        else:
            tp2_pct = tp1_pct * 1.5
        take_profit = price * (1 + tp1_pct / 100)
        tp2_price_out = price * (1 + tp2_pct / 100)
    else:
        swing_ref = score_result.get("swing_high_recent")
        if swing_ref and swing_ref > price:
            struct_dist = (swing_ref * 1.001 - price) / price * 100
            sl_pct = max(struct_dist, atr_pct * 1.0)
            sl_basis = "结构高点"
        else:
            sl_pct = atr_pct * 1.5
        sl_pct = min(sl_pct, atr_pct * 3.0)
        sl_pct = max(sl_pct, 0.5) * conf_mult
        stop_loss = price * (1 + sl_pct / 100)
        tp1_price = score_result.get("support")
        if tp1_price and 0 < tp1_price < price:
            tp1_pct = (price - tp1_price) / price * 100
        else:
            tp1_pct = sl_pct * 2
        tp2_price = score_result.get("low_24h")
        if tp2_price and 0 < tp2_price < price * (1 - tp1_pct / 100) * 0.999:
            tp2_pct = (price - tp2_price) / price * 100
        else:
            tp2_pct = tp1_pct * 1.5
        take_profit = price * (1 - tp1_pct / 100)
        tp2_price_out = price * (1 - tp2_pct / 100)

    rr_ratio = round(tp1_pct / sl_pct, 1) if sl_pct > 0 else 0

    risk_amount = account_balance * 0.02
    position_size = risk_amount / (sl_pct / 100) if sl_pct > 0 else 0
    position_pct = position_size / account_balance * 100 if account_balance > 0 else 0
    if score_confidence < 75:
        position_pct *= 0.6

    if atr_pct < 1:
        leverage = 5
    elif atr_pct < 2:
        leverage = 3
    elif atr_pct < 4:
        leverage = 2
    else:
        leverage = 1
    if score_confidence < 65:
        leverage = max(int(leverage * 0.5), 1)

    notional_value = position_size
    if notional_value > account_balance * 2:
        notional_value = account_balance * 2
        position_pct = 200.0

    return {
        "direction": direction,
        "confidence": round(score_confidence, 1),
        "entry_price": price,
        "stop_loss": round(stop_loss, 6) if price < 1000 else round(stop_loss, 2),
        "take_profit": round(take_profit, 6) if price < 1000 else round(take_profit, 2),
        "sl_pct": round(sl_pct, 2),
        "tp_pct": round(tp1_pct, 2),
        "rr_ratio": rr_ratio,
        "tp2_price": round(tp2_price_out, 6) if price < 1000 else round(tp2_price_out, 2),
        "sl_basis": sl_basis,
        "leverage": leverage,
        "position_pct": round(position_pct, 1),
        "notional_value": round(notional_value, 2),
        "risk_amount": round(risk_amount, 2),
    }


def format_output(symbol: str, score: dict, risk: dict, price: float, brief: bool = False) -> str:
    lines = []
    now_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

    grade_emoji = {"A": "🅰️", "B": "🅱️", "C": "©️", "D": "⚪", "N": "🚫"}
    grade_tag = f"{grade_emoji.get(score['grade'], '⚪')} {score['grade']}"

    lines.append(f"{'='*56}")
    lines.append(f"  🦐 币安信号雷达  |  {symbol.upper()}  |  {now_str}")
    lines.append(f"  信号等级: {grade_tag}")
    status_tag = {"NO_TRADE": "🚫 NO TRADE", "READY": "✅ READY", "WAIT": "⏸ WAIT"}
    lines.append(f"  信号状态: {status_tag.get(score.get('signal_status', 'WAIT'), '⏸ WAIT')}")
    lines.append(f"  🌍 环境: {score.get('market_regime','RANGE')} (4H:{score.get('trend_4h','—')}/1H:{score.get('trend_1h','—')} | BTC:{score.get('btc_regime','—')})")
    if score.get("no_trade"):
        lines.append(f"  🚫 {score.get('no_trade_reason','')}")
    elif score.get("signal_status") == "WAIT" and score.get("trigger_missing"):
        lines.append(f"  ⏳ 缺: {', '.join(score['trigger_missing'][:4])}")
    lines.append(f"{'='*56}")

    if not brief:
        change = score["change_24h"]
        change_str = f"{change:+.2f}%"
        h24 = score["high_24h"]
        l24 = score["low_24h"]

        lines.append(f"  💰 当前价: ${price:,.4f}  (24h: {change_str})")
        if h24 > 0 and l24 > 0:
            lines.append(f"     24h 高: ${h24:,.4f}  低: ${l24:,.4f}")
        lines.append("")
        lines.append(f"  📊 多空概率")
        long_bars = int(score["long_probability"] / 5)
        short_bars = int(score["short_probability"] / 5)
        lines.append(f"     🟢 做多: {score['long_probability']}%  {'■' * long_bars}")
        lines.append(f"     🔴 做空: {score['short_probability']}%  {'■' * short_bars}")
        lines.append("")
        lines.append(f"  📈 技术指标")
        lines.append(f"     RSI(14): {score['rsi']}  |  EMA20: ${score['ema20']:,.4f}  |  EMA50: ${score['ema50']:,.4f}")
        lines.append(f"     结构: {score['structure']}  |  ATR: {score['atr_pct']:.2f}%  |  MACD: {score['macd_trend']}")
        lines.append(f"     量比: x{score['volume_ratio']:.1f}  |  资金费率: {score['funding_rate']:+.6f}  |  共振: {'✅' if score['tf_aligned'] else '❌'}")
        if score["fib_nearest"]:
            lines.append(f"     Fib靠近: {score['fib_nearest']}")
        lines.append(f"     区间百分位: {score['range_percentile']:.0f}%  |  S/R盈亏比: {str(score['rr_metric']):>6}")
        s = score.get("support")
        r = score.get("resistance")
        sd = score.get("sup_dist_pct")
        rd = score.get("res_dist_pct")
        s_str = f"支撑 ${s:,.4f} (-{sd}%)" if s is not None else "支撑 —"
        if r is not None:
            r_str = f"阻力 ${r:,.4f} (+{rd}%)"
        else:
            h24 = score["high_24h"]
            if h24 > price:
                r_str = f"阻力 ${h24:,.4f} (+{(h24-price)/price*100:.1f}%, 24h高)"
            else:
                r_str = "阻力 —"
        lines.append(f"     S/R: {s_str}  |  {r_str}")
        lines.append("")
        ws = score.get("whale_score", 50)
        wl = score.get("whale_grade_label", "中性 ➖")
        bars_ws = int(ws / 10)
        ws_emoji = "🐋🔴" if ws < 40 else ("🐋🟢" if ws > 60 else "🐋⚪")
        lines.append(f"  {ws_emoji} 巨鲸评分: {ws:.0f}/100  {wl}")
        lines.append(f"     {'■' * bars_ws}{'░' * (10 - bars_ws)}")
        # 5因子详细分解
        factor_map = {
            "exchange": ("🏦 Taker资金流", "20%"),
            "holding": ("🐳 持仓变化", "20%"),
            "transfer": ("🔄 大额转账", "15%"),
            "concentration": ("🎯 集中度", "5%"),
            "smart_money": ("🧠 聪明钱", "30%"),
            "orderbook": ("📚 订单簿", "10%"),
        }
        factors = score.get("whale_factors", {})
        for fkey, (flabel, fweight) in factor_map.items():
            f = factors.get(fkey, {})
            fs = f.get("score", 50)
            fd = f.get("detail", "")
            f_bars = int(fs / 10)
            f_emoji = "🔴" if fs < 40 else ("🟢" if fs > 60 else "⚪")
            if fd:
                lines.append(f"     {f_emoji} {flabel:<10} {fs:3d} {fweight}  {fd}")
            else:
                lines.append(f"     {f_emoji} {flabel:<10} {fs:3d} {fweight}  —")

    lines.append("")
    if score["reasons_long"]:
        lines.append(f"  🟢 做多理由 ({score['long_score']}分)")
        for r_text in score["reasons_long"]:
            lines.append(f"     ✓ {r_text}")
    if score["reasons_short"]:
        lines.append(f"  🔴 做空理由 ({score['short_score']}分)")
        for r_text in score["reasons_short"]:
            lines.append(f"     ✓ {r_text}")
    if score["warnings"]:
        lines.append(f"  ⚠️ 风险提示")
        for w in score["warnings"]:
            lines.append(f"     · {w}")

    lines.append("")
    lines.append(f"  {'='*52}")

    dir_emoji = {"LONG": "🟢 做多", "SHORT": "🔴 做空", "NEUTRAL": "⚪ 观望"}

    if risk["direction"] == "NEUTRAL":
        lines.append(f"  📋 操作建议  [{grade_tag}]")
        lines.append(f"  {'='*52}")
        if score.get("no_trade"):
            lines.append(f"     建议: 🚫 NO TRADE（{score.get('no_trade_reason','')}）")
        elif score.get("signal_status") == "WAIT":
            lines.append(f"     建议: ⏸ WAIT（候选 {score.get('candidate_direction','—')}，条件未满足）")
            if score.get("trigger_missing"):
                lines.append(f"     缺: {', '.join(score['trigger_missing'][:4])}")
        else:
            lines.append(f"     建议: ⚪ 观望（无 READY 信号）")
        lines.append(f"     置信度: {risk['confidence']}%")
        lines.append("")
        lines.append(f"     💡 等待以下条件改善后再入场：")
        lines.append(f"        · 趋势更明确（EMA排列清晰）")
        lines.append(f"        · 结构更完整（HH/HL或LH/LL成立）")
        lines.append(f"        · K线形态确认")
        lines.append(f"        · 量能配合")
    else:
        sl_sign = f"-{risk['sl_pct']:.2f}%"
        tp_sign = f"+{risk['tp_pct']:.2f}%"
        lines.append(f"  📋 操作建议  [{grade_tag}]")
        lines.append(f"  {'='*52}")
        lines.append(f"     方向: {dir_emoji.get(risk['direction'], risk['direction'])}")
        lines.append(f"     置信度: {risk['confidence']}%")
        lines.append(f"     入场价: ${risk['entry_price']:,.6f}")
        lines.append(f"     止损价: ${risk['stop_loss']:,.6f}  ({sl_sign})  依据: {risk.get('sl_basis','ATR')}")
        lines.append(f"     止盈价: ${risk['take_profit']:,.6f}  ({tp_sign})")
        lines.append(f"     TP2: ${risk.get('tp2_price',0):,.6f}  (下一流动性目标)")
        lines.append(f"     盈亏比: 1:{risk['rr_ratio']}")
        lines.append(f"     建议杠杆: {risk['leverage']}x  (保证金{risk['position_pct']}%)")
        lines.append(f"     名义仓位: ${risk['notional_value']:,.2f}")
        lines.append(f"     每单最大亏损: ${risk['risk_amount']:.2f}  (本金2.0%)")
        if risk["leverage"] >= 8:
            lines.append(f"     ⚠️ 高杠杆 ({risk['leverage']}x)，注意波动风险")
        if risk["position_pct"] > 20:
            lines.append(f"     ⚠️ 保证金占用较高，建议分批入场")

    lines.append("")
    lines.append(f"  {'─'*52}")
    lines.append(f"  💡 严格执行: 入场前检查所有A+条件")
    lines.append(f"     没有信号 = 空仓")
    lines.append(f"  {'='*52}")

    return "\n".join(lines)


def analyze_coin(symbol: str, balance: float = 1000.0) -> str:
    """
    对外暴露的分析函数（返回格式化文本）。

    :param symbol: 币种名称，例如 "BTC" "ETHUSDT" "OPG"
    :param balance: 账户本金 (USDT)
    :return: 格式化的分析结果文本
    """
    raw = symbol.upper().strip()
    sym = raw if raw.endswith("USDT") else raw + "USDT"

    try:
        ticker = fetch_ticker(sym)
        price = float(ticker["lastPrice"])

        klines_15m = fetch_klines(sym, "15m", 200, closed_only=True)
        klines_5m = fetch_klines(sym, "5m", 100, closed_only=True)
        klines_1m = fetch_klines(sym, "1m", 60, closed_only=True)
        klines_4h = fetch_klines(sym, "4h", 200, closed_only=True)
        klines_1h = fetch_klines(sym, "1h", 200, closed_only=True)

        funding = fetch_funding_rate(sym)

        # V2.0: 衍生品上下文 + Market Regime + BTC 大盘环境
        derivatives = build_derivatives_context(sym)
        regime = detect_market_regime(klines_4h, klines_1h)
        btc_regime = None
        try:
            btc_4h = fetch_klines("BTCUSDT", "4h", 200, closed_only=True)
            btc_1h = fetch_klines("BTCUSDT", "1h", 200, closed_only=True)
            btc_regime = detect_market_regime(btc_4h, btc_1h)
        except Exception:
            btc_regime = None

        score = score_system(price, klines_15m, klines_5m, klines_1m, ticker, funding, sym,
                             derivatives=derivatives, regime=regime, btc_regime=btc_regime)
        risk = risk_recommendation(price, score, balance)
        if score.get("no_trade"):
            risk["direction"] = "NEUTRAL"

        return format_output(sym, score, risk, price, brief=False)

    except Exception as e:
        return f"❌ 分析失败: {e}\n   可能原因：币种不存在、网络异常、API限制"


def analyze_coin_dict(symbol: str, balance: float = 1000.0) -> dict:
    """
    对外暴露的分析函数（返回结构化数据，供 GUI 美化展示）。

    :param symbol: 币种名称，例如 "BTC" "ETHUSDT" "OPG"
    :param balance: 账户本金 (USDT)
    :return: dict with keys: symbol, price, grade, direction, confidence,
             entry_price, stop_loss, take_profit, reasons_long, reasons_short,
             warnings, atr, rsi, macd, volume_ratio, leverage, etc.
    """
    raw = symbol.upper().strip()
    sym = raw if raw.endswith("USDT") else raw + "USDT"

    try:
        ticker = fetch_ticker(sym)
        price = float(ticker["lastPrice"])

        klines_15m = fetch_klines(sym, "15m", 200, closed_only=True)
        klines_5m = fetch_klines(sym, "5m", 100, closed_only=True)
        klines_1m = fetch_klines(sym, "1m", 60, closed_only=True)
        klines_4h = fetch_klines(sym, "4h", 200, closed_only=True)
        klines_1h = fetch_klines(sym, "1h", 200, closed_only=True)

        funding = fetch_funding_rate(sym)

        # V2.0: 衍生品上下文 + Market Regime + BTC 大盘环境
        derivatives = build_derivatives_context(sym)
        regime = detect_market_regime(klines_4h, klines_1h)
        btc_regime = None
        try:
            btc_4h = fetch_klines("BTCUSDT", "4h", 200, closed_only=True)
            btc_1h = fetch_klines("BTCUSDT", "1h", 200, closed_only=True)
            btc_regime = detect_market_regime(btc_4h, btc_1h)
        except Exception:
            btc_regime = None

        score = score_system(price, klines_15m, klines_5m, klines_1m, ticker, funding, sym,
                             derivatives=derivatives, regime=regime, btc_regime=btc_regime)
        risk = risk_recommendation(price, score, balance)
        if score.get("no_trade"):
            risk["direction"] = "NEUTRAL"

        # V2.0: 信号审计（记录有入场建议的信号 + 评估历史信号到期窗口）
        try:
            from signal_audit import save_signal, evaluate_pending
            save_signal({
                "id": f"{int(time.time() * 1000)}-{sym}",
                "time": int(time.time() * 1000),
                "symbol": sym,
                "direction": risk["direction"],
                "price": price,
                "entry": risk.get("entry_price"),
                "stop_loss": risk.get("stop_loss"),
                "take_profit": risk.get("take_profit"),
                "sl_pct": risk.get("sl_pct"),
                "tp_pct": risk.get("tp_pct"),
                "regime": score.get("market_regime"),
                "grade": score.get("grade"),
                "long_score": score.get("long_score"),
                "short_score": score.get("short_score"),
                "rsi": score.get("rsi"),
                "volume_ratio": score.get("volume_ratio"),
                "oi_trend": score.get("oi_trend"),
                "funding_regime": score.get("funding_regime"),
                "taker_trend": score.get("taker_trend"),
                "entry_trigger": score.get("entry_trigger"),
                "strategy": score.get("strategy"),
                "setup": score.get("setup"),
                "candidate_direction": score.get("candidate_direction"),
                "trade_direction": score.get("trade_direction"),
                "signal_status": score.get("signal_status"),
                "reason_codes": score.get("reason_codes", []),
                "regime_strength": score.get("regime_strength"),
                "volatility_state": score.get("volatility_state"),
            })
            evaluate_pending()
        except Exception:
            pass  # 审计失败不影响主流程

        grade_emojis = {"A": "🅰️", "B": "🅱️", "C": "©️", "D": "⚪", "N": "🚫"}  # emoji only, label in GUI's GRADE_NAMES
        dir_labels = {"LONG": "🟢 看多", "SHORT": "🔴 看空", "NEUTRAL": "⚪ 观望"}

        return {
            "symbol": sym,
            "price": price,
            "change_24h": score.get("change_24h", 0),
            "high_24h": score.get("high_24h", 0),
            "low_24h": score.get("low_24h", 0),
            "grade": score["grade"],
            "grade_emoji": grade_emojis.get(score["grade"], "⚪"),
            "direction": risk["direction"],
            "direction_label": dir_labels.get(risk["direction"], "⚪ 观望"),
            "confidence": risk["confidence"],
            "entry_price": risk["entry_price"],
            "stop_loss": risk["stop_loss"],
            "take_profit": risk["take_profit"],
            "sl_pct": risk["sl_pct"],
            "tp_pct": risk["tp_pct"],
            "rr_ratio": risk["rr_ratio"],
            "tp2_price": risk.get("tp2_price"),
            "sl_basis": risk.get("sl_basis", "ATR"),
            "leverage": risk["leverage"],
            "position_pct": risk["position_pct"],
            "notional_value": risk["notional_value"],
            "risk_amount": risk["risk_amount"],
            "long_prob": score["long_probability"],
            "short_prob": score["short_probability"],
            "long_score": score["long_score"],
            "short_score": score["short_score"],
            "rsi": score["rsi"],
            "ema20": score["ema20"],
            "ema50": score["ema50"],
            "atr_pct": score["atr_pct"],
            "macd_trend": score["macd_trend"],
            "structure": score["structure"],
            "volume_ratio": score["volume_ratio"],
            "funding_rate": score["funding_rate"],
            "tf_aligned": score["tf_aligned"],
            "reasons_long": score["reasons_long"],
            "reasons_short": score["reasons_short"],
            "warnings": score["warnings"],
            "range_percentile": score["range_percentile"],
            "fib_nearest": score.get("fib_nearest"),
            "rr_metric": score.get("rr_metric", "-"),
            "support": score.get("support"),
            "resistance": score.get("resistance"),
            "sup_dist_pct": score.get("sup_dist_pct"),
            "res_dist_pct": score.get("res_dist_pct"),
            "whale_score": score.get("whale_score", 50.0),
            "whale_grade_label": score.get("whale_grade_label", "中性 ➖"),
            "whale_factors": score.get("whale_factors", {}),
            # ── V2.0 新增 ──
            "market_regime": score.get("market_regime", "RANGE"),
            "trend_4h": score.get("trend_4h", "FLAT"),
            "trend_1h": score.get("trend_1h", "FLAT"),
            "regime_reasons": score.get("regime_reasons", []),
            "btc_regime": score.get("btc_regime", "RANGE"),
            "no_trade": score.get("no_trade", False),
            "no_trade_reason": score.get("no_trade_reason", ""),
            "entry_trigger": score.get("entry_trigger", "WAIT"),
            "signal_status": score.get("signal_status", "WAIT"),
            "trigger_met": score.get("trigger_met", []),
            "trigger_missing": score.get("trigger_missing", []),
            "oi": score.get("oi"),
            "oi_change_5m": score.get("oi_change_5m"),
            "oi_change_15m": score.get("oi_change_15m"),
            "oi_change_1h": score.get("oi_change_1h"),
            "oi_trend": score.get("oi_trend"),
            "funding_trend": score.get("funding_trend"),
            "funding_regime": score.get("funding_regime"),
            "taker_ratio": score.get("taker_ratio"),
            "taker_trend": score.get("taker_trend"),
            "global_ls_ratio": score.get("global_ls_ratio"),
            "liq_5m_long": score.get("liq_5m_long"),
            "liq_5m_short": score.get("liq_5m_short"),
            # ── V2.1 新增 ──
            "strategy": score.get("strategy", "NONE"),
            "setup": score.get("setup", "NONE"),
            "candidate_direction": score.get("candidate_direction", "NEUTRAL"),
            "trade_direction": score.get("trade_direction", "NEUTRAL"),
            "counter_trend": score.get("counter_trend", False),
            "reason_codes": score.get("reason_codes", []),
            "regime_strength": score.get("regime_strength", 0.0),
            "volatility_state": score.get("volatility_state", "UNKNOWN"),
            "probability_note": score.get("probability_note", ""),
            "direction_score": score.get("direction_score", 0),
            "score_confidence": score.get("score_confidence", 0),
            "need_confirm": score.get("need_confirm", 0),
            "confirm_count": score.get("confirm_count", 0),
        }

    except Exception as e:
        return {"error": f"❌ 分析失败: {e}"}
