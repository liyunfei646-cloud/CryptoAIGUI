#!/usr/bin/env python3
"""
币安超短线信号雷达 — 半自动化分析脚本
======================================
基于六层过滤框架：趋势 → 结构 → 斐波那契 → K线确认 → 量能 → 风控
零外部依赖（仅用Python标准库 + Binance公开API）

用法:
  python3 binance_scalping_analyzer.py <币种>
  例: python3 binance_scalping_analyzer.py BTCUSDT
       python3 binance_scalping_analyzer.py ETHUSDT
       python3 binance_scalping_analyzer.py KERNELUSDT
"""

import json
import math
import sys
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone, timedelta

# ─── 全局配置 ────────────────────────────────────────────────────────────────
# 优先使用合约(fapi)API，兼容现货(api/v3)备用
BINANCE_SPOT = "https://api.binance.com"
BINANCE_FUTURES = "https://fapi.binance.com"

# 时区
TZ = timezone(timedelta(hours=8))

# ─── 1. 数据获取（Binance公开K线API）────────────────────────────────────────

def _http_get(url: str, timeout: int = 15) -> bytes:
    """带重试和SSL降级的HTTP GET"""
    import ssl
    ctx = ssl.create_default_context()
    # 部分服务器SSL协商有问题，放宽限制
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
    raise last_err  # type: ignore


def fetch_klines(symbol: str, interval: str, limit: int = 200) -> list[dict]:
    """
    从Binance获取K线数据。
    优先合约API(fapi)，回退现货API(api/v3)。
    """
    sym = symbol.upper()
    # 尝试合约
    fapi_url = (f"{BINANCE_FUTURES}/fapi/v1/klines"
                f"?symbol={sym}&interval={interval}&limit={limit}")
    spot_url = (f"{BINANCE_SPOT}/api/v3/klines"
                f"?symbol={sym}&interval={interval}&limit={limit}")

    raw = None
    for url in (fapi_url, spot_url):
        try:
            raw = json.loads(_http_get(url).decode("utf-8"))
            break
        except Exception:
            continue

    if raw is None:
        raise ConnectionError(f"无法获取 {sym} K线数据（合约和现货均失败）")

    klines = []
    for k in raw:
        klines.append({
            "time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return klines


def fetch_ticker(symbol: str) -> dict:
    """获取当前价格及24h统计（合约→现货回退）"""
    sym = symbol.upper()
    fapi_url = f"{BINANCE_FUTURES}/fapi/v1/ticker/24hr?symbol={sym}"
    spot_url = f"{BINANCE_SPOT}/api/v3/ticker/24hr?symbol={sym}"

    for url in (fapi_url, spot_url):
        try:
            return json.loads(_http_get(url).decode("utf-8"))
        except Exception:
            continue
    raise ConnectionError(f"无法获取 {sym} 行情数据（合约和现货均失败）")


# ─── 2. 技术指标计算 ──────────────────────────────────────────────────────

def ema(values: list[float], period: int) -> float:
    """计算指数移动平均（当前值）"""
    if len(values) < period:
        return values[-1]
    multiplier = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    for v in values[period:]:
        result = (v - result) * multiplier + result
    return result


def ema_series(values: list[float], period: int) -> list[float]:
    """返回完整的EMA序列"""
    if len(values) < period:
        return [values[-1]] * len(values)
    multiplier = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    series = [result]
    for v in values[period:]:
        result = (v - result) * multiplier + result
        series.append(result)
    # padding
    padding = [series[0]] * (period - 1)
    return padding + series


def sma(values: list[float], period: int) -> float:
    if len(values) < period:
        return sum(values) / len(values)
    return sum(values[-period:]) / period


def rsi(values: list[float], period: int = 14) -> float:
    """RSI指标"""
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
    """MACD: MACD线, 信号线, 柱状图, 背离检测"""
    if len(values) < 35:
        return {"macd": 0, "signal": 0, "histogram": 0, "hist_trend": "neutral"}
    ema12 = ema_series(values, 12)
    ema26 = ema_series(values, 26)
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    signal_line = ema_series(macd_line, 9)
    histogram = macd_line[-1] - signal_line[-1]

    # 柱状图趋势（最近3个histogram值）
    hist_series = [macd_line[i] - signal_line[i] for i in range(-3, 0)] if len(macd_line) >= 3 else [0]
    if len(hist_series) >= 3:
        h1, h2, h3 = hist_series
        hist_trend = "rising" if h3 > h2 > h1 else ("falling" if h3 < h2 < h1 else "neutral")
    else:
        hist_trend = "neutral"

    return {
        "macd": macd_line[-1],
        "signal": signal_line[-1],
        "histogram": histogram,
        "hist_trend": hist_trend,
        "macd_line_series": macd_line[-5:],
    }


def find_support_resistance(klines: list[dict], swing_highs: list, swing_lows: list) -> dict:
    """
    从摆动点找最近的支撑和阻力。
    """
    price = klines[-1]["close"]

    resistances = sorted(set(h[1] for h in swing_highs), reverse=True)
    supports = sorted(set(l[1] for l in swing_lows))

    # 最近的阻力（高于价格的最低阻力）
    nearest_resistance = None
    for r in resistances:
        if r > price:
            nearest_resistance = r
            break

    # 最近的支撑（低于价格的最高支撑）
    nearest_support = None
    for s in supports:
        if s < price:
            nearest_support = s
            break

    return {
        "support": nearest_support,
        "resistance": nearest_resistance,
        "sup_dist_pct": round((price - nearest_support) / price * 100, 2) if nearest_support else None,
        "res_dist_pct": round((nearest_resistance - price) / price * 100, 2) if nearest_resistance else None,
    }


def atr(klines: list[dict], period: int = 14) -> float:
    """平均真实波幅"""
    if len(klines) < period + 1:
        return (klines[-1]["high"] - klines[-1]["low"]) * 0.01
    tr_values = []
    for i in range(1, len(klines)):
        high, low = klines[i]["high"], klines[i]["low"]
        prev_close = klines[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)
    return sma(tr_values, period)


# ─── 3. 结构识别 ────────────────────────────────────────────────────────

def find_swing_points(klines: list[dict], window: int = 5) -> dict:
    """
    识别摆动高点和低点。
    返回 {swing_highs: [(index, price), ...], swing_lows: [...]}
    """
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    swing_highs = []
    swing_lows = []

    for i in range(window, len(klines) - window):
        # 摆动高点：左右window根K线中最高
        left = highs[i - window:i]
        right = highs[i + 1:i + window + 1]
        if highs[i] > max(left) and highs[i] > max(right):
            swing_highs.append((i, highs[i]))

        # 摆动低点
        left = lows[i - window:i]
        right = lows[i + 1:i + window + 1]
        if lows[i] < min(left) and lows[i] < min(right):
            swing_lows.append((i, lows[i]))

    return {"swing_highs": swing_highs, "swing_lows": swing_lows}


def analyze_structure(klines: list[dict], swing_highs: list, swing_lows: list) -> dict:
    """
    分析市场结构：Higher High (HH), Higher Low (HL) = 上涨
                     Lower High (LH), Lower Low (LL) = 下跌
    """
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
        trend = "bearish_div"    # 顶背离雏形
    elif ll and not lh:
        trend = "bullish_div"    # 底背离雏形
    else:
        trend = "neutral"

    return {
        "trend": trend,
        "recent_highs": recent_highs,
        "recent_lows": recent_lows,
        "detail": {
            "higher_high": hh,
            "higher_low": hl,
            "lower_high": lh,
            "lower_low": ll,
        }
    }


# ─── 4. 斐波那契 ────────────────────────────────────────────────────────

def fibonacci_levels(high: float, low: float) -> dict:
    """计算斐波那契回撤位"""
    diff = high - low
    return {
        "0.000": high,
        "0.236": high - diff * 0.236,
        "0.382": high - diff * 0.382,
        "0.500": high - diff * 0.500,
        "0.618": high - diff * 0.618,
        "0.786": high - diff * 0.786,
        "1.000": low,
    }


def find_nearest_fib(current_price: float, fib_levels: dict, tolerance_pct: float = 0.003) -> dict:
    """
    检测价格是否靠近某个斐波那契回调位。
    tolerance_pct: 容忍度（百分比），默认0.3%
    """
    result = {"nearest": None, "distance_pct": None, "touch": False}
    for name, level in fib_levels.items():
        dist = abs(current_price - level) / current_price
        if result["nearest"] is None or dist < result["distance_pct"]:
            result["nearest"] = name
            result["distance_pct"] = dist
            result["touch"] = dist < tolerance_pct
    return result


# ─── 5. K线形态识别 ────────────────────────────────────────────────────

def detect_candlestick_patterns(klines: list[dict]) -> dict:
    """
    识别最近2-3根K线的形态。
    """
    if len(klines) < 3:
        return {}

    c1, c2, c3 = klines[-3], klines[-2], klines[-1]  # c1=旧, c3=最新

    patterns = {}

    # 吞没形态（Bullish Engulfing）
    bullish_engulf = (
        c2["close"] > c2["open"] and
        c1["close"] < c1["open"] and
        c2["close"] > c1["open"] and
        c2["open"] < c1["close"]
    )
    patterns["bullish_engulfing"] = bullish_engulf

    # 看跌吞没
    bearish_engulf = (
        c2["close"] < c2["open"] and
        c1["close"] > c1["open"] and
        c2["close"] < c1["open"] and
        c2["open"] > c1["close"]
    )
    patterns["bearish_engulfing"] = bearish_engulf

    # Pin Bar (锤子线/上吊线)
    for idx, c in [("c2", c2), ("c3", c3)]:
        body = abs(c["close"] - c["open"])
        upper_wick = c["high"] - max(c["open"], c["close"])
        lower_wick = min(c["open"], c["close"]) - c["low"]
        total = c["high"] - c["low"]
        if total == 0:
            continue

        # 下影线Pin Bar（看涨信号）
        if lower_wick > body * 2 and upper_wick < body * 0.5:
            patterns[f"pin_bar_bullish_{idx}"] = True
        # 上影线Pin Bar（看跌信号）
        if upper_wick > body * 2 and lower_wick < body * 0.5:
            patterns[f"pin_bar_bearish_{idx}"] = True

    # 十字星/Doji
    for idx, c in [("c2", c2), ("c3", c3)]:
        body = abs(c["close"] - c["open"])
        total = c["high"] - c["low"]
        if total > 0 and body / total < 0.1:
            patterns[f"doji_{idx}"] = True

    # 假突破（价格突破前低/前高后收回）
    prev_low = min(c1["low"], c2["low"])
    prev_high = max(c1["high"], c2["high"])
    if c3["low"] < prev_low and c3["close"] > prev_low:
        patterns["fake_break_below"] = True  # 假跌破 → 看涨
    if c3["high"] > prev_high and c3["close"] < prev_high:
        patterns["fake_break_above"] = True  # 假突破 → 看跌

    return patterns


# ─── 6. 量能分析 ────────────────────────────────────────────────────────

def analyze_volume(klines: list[dict]) -> dict:
    """
    分析成交量：放量、缩量、异常量柱
    """
    volumes = [k["volume"] for k in klines]
    if len(volumes) < 20:
        return {"signal": "neutral", "volume_ratio": 1.0}

    avg_volume = sma(volumes, 20)
    last_vol = volumes[-1]
    vol_ratio = last_vol / avg_volume if avg_volume > 0 else 1.0

    # 最近3根量能趋势
    recent_vols = volumes[-3:]
    vol_trend = "increasing" if recent_vols[-1] > recent_vols[0] else "decreasing"

    result = {
        "volume_ma20": avg_volume,
        "last_volume": last_vol,
        "volume_ratio": vol_ratio,
        "vol_trend": vol_trend,
        "signal": "neutral",
    }

    if vol_ratio > 1.5:
        result["signal"] = "volume_spike"
    elif vol_ratio < 0.5:
        result["signal"] = "volume_shrink"

    return result


# ─── 7. 资金费率提示 ──────────────────────────────────────────────────

def fetch_funding_rate(symbol: str) -> dict:
    """获取最近资金费率（币安合约API）"""
    try:
        url = (f"{BINANCE_FUTURES}/fapi/v1/fundingRate"
               f"?symbol={symbol.upper()}&limit=3")
        data = json.loads(_http_get(url, timeout=8).decode("utf-8"))
        if data:
            latest = float(data[-1]["fundingRate"])
            return {
                "funding_rate": latest,
                "annualized": latest * 3 * 365 * 100,  # 8h费率 → 年化%
                "signal": "long_crowded" if latest > 0.001 else (
                    "short_crowded" if latest < -0.001 else "neutral"
                )
            }
    except Exception:
        pass
    return {"funding_rate": 0, "annualized": 0, "signal": "neutral"}


# ─── 8. 综合评分引擎 ──────────────────────────────────────────────────

def score_system(
    price: float,
    klines_15m: list[dict],
    klines_5m: list[dict],
    klines_1m: list[dict],
    ticker: dict,
    funding: dict,
) -> dict:
    """核心评分引擎——返回做多/做空概率及建议"""

    # ── 提取数据 ──
    closes_15m = [k["close"] for k in klines_15m]
    highs_15m = [k["high"] for k in klines_15m]
    lows_15m = [k["low"] for k in klines_15m]
    closes_5m = [k["close"] for k in klines_5m]
    closes_1m = [k["close"] for k in klines_1m]

    # ── Trend: EMA ──
    ema20_15m = ema(closes_15m, 20)
    ema50_15m = ema(closes_15m, 50)
    ema200_15m = ema(closes_15m, 200) if len(closes_15m) >= 200 else ema(closes_15m, len(closes_15m) // 2)

    # ── Trend: RSI ──
    rsi_val = rsi(closes_15m, 14)

    # ── MACD ──
    macd_data = macd(closes_15m)

    # ── ATR ──
    atr_15m = atr(klines_15m, 14)
    atr_pct = atr_15m / price * 100

    # ── 结构 ──
    swings = find_swing_points(klines_15m, window=5)
    structure = analyze_structure(klines_15m, swings["swing_highs"], swings["swing_lows"])

    # ── S/R 支撑阻力 ──
    sr = find_support_resistance(klines_15m, swings["swing_highs"], swings["swing_lows"])

    # ── 斐波那契 ──
    recent_high = max(highs_15m[-30:])
    recent_low = min(lows_15m[-30:])
    fibs = fibonacci_levels(recent_high, recent_low)
    fib_check = find_nearest_fib(price, fibs)

    # ── K线形态 ──
    patterns_15m = detect_candlestick_patterns(klines_15m)
    patterns_5m = detect_candlestick_patterns(klines_5m)

    # ── 量能 ──
    vol_analysis = analyze_volume(klines_15m)

    # ── 多时间周期趋势共振 ──
    ema20_5m = ema(closes_5m, 20)
    ema50_5m = ema(closes_5m, 50)
    ema20_1m = ema(closes_1m, 20)
    ema50_1m = ema(closes_1m, 50)

    trend_15m = "bull" if ema20_15m > ema50_15m else "bear"
    trend_5m = "bull" if ema20_5m > ema50_5m else "bear"
    trend_1m = "bull" if ema20_1m > ema50_1m else "bear"
    tf_aligned = (trend_15m == trend_5m == trend_1m)
    tf_dominant = trend_15m  # 以15m为主

    # ── 24h变化 ──
    change_24h = float(ticker.get("priceChangePercent", 0))

    # ════════════════════════════════════════════════════════════════
    # 评分计算（净得分制 -- 每个条件只支持一方，不加基础分）
    # 避免中性条件稀释信号
    # ════════════════════════════════════════════════════════════════

    long_score = 0
    short_score = 0

    reasons_long = []    # 做多理由
    reasons_short = []   # 做空理由
    warnings = []        # 风险提示

    # ── EMA趋势 ──
    if ema20_15m > ema50_15m > ema200_15m:
        long_score += 18
        reasons_long.append("EMA多头排列 (20>50>200)")
    elif ema20_15m < ema50_15m < ema200_15m:
        short_score += 18
        reasons_short.append("EMA空头排列 (20<50<200)")
    elif ema20_15m > ema50_15m:
        long_score += 10
        reasons_long.append("EMA20在EMA50上方")
    else:
        short_score += 10
        reasons_short.append("EMA20在EMA50下方")

    # ── 价格相对EMA位置 ──
    if price > ema20_15m * 1.005:
        long_score += 6
        reasons_long.append("价格在EMA20上方运行")
    elif price < ema20_15m * 0.995:
        short_score += 6
        reasons_short.append("价格在EMA20下方运行")

    # ── 结构判断 ──
    struct_trend = structure["trend"]
    if struct_trend == "bullish":
        long_score += 18
        reasons_long.append("上涨结构 (HH+HL)")
    elif struct_trend == "bearish":
        short_score += 18
        reasons_short.append("下跌结构 (LH+LL)")
    elif struct_trend == "bearish_div":
        short_score += 8
        reasons_short.append("顶背离风险")
    elif struct_trend == "bullish_div":
        long_score += 8
        reasons_long.append("底背离可能")

    # ── RSI (不再给双方同时加分) ──
    if rsi_val < 30:
        long_score += 12
        reasons_long.append(f"RSI超卖 ({rsi_val:.1f})")
        warnings.append("RSI超卖区，短期反弹概率大但趋势仍可能向下")
    elif rsi_val > 70:
        short_score += 12
        reasons_short.append(f"RSI超买 ({rsi_val:.1f})")
        warnings.append("RSI超买区，短期回调概率大但趋势仍可能向上")

    # ── 斐波那契支撑/阻力 ──
    if fib_check["touch"]:
        fib_name = fib_check["nearest"]
        if float(fib_name) >= 0.618:  # 深回调位
            if struct_trend in ("bullish", "neutral"):
                long_score += 10
                reasons_long.append(f"回踩Fib {fib_name} 支撑位")
            else:
                short_score += 8
                reasons_short.append(f"回抽Fib {fib_name} 阻力位")
        elif float(fib_name) <= 0.382:
            if struct_trend in ("bearish", "neutral"):
                short_score += 10
                reasons_short.append(f"反抽Fib {fib_name} 阻力位")
            else:
                long_score += 8
                reasons_long.append(f"回踩Fib {fib_name} 支撑位")

    # ── K线形态 ──
    if patterns_15m.get("bullish_engulfing") or patterns_5m.get("bullish_engulfing"):
        long_score += 12
        reasons_long.append("看涨吞没形态")
    if patterns_15m.get("bearish_engulfing") or patterns_5m.get("bearish_engulfing"):
        short_score += 12
        reasons_short.append("看跌吞没形态")

    if patterns_15m.get("pin_bar_bullish_c2") or patterns_15m.get("pin_bar_bullish_c3"):
        long_score += 8
        reasons_long.append("下影Pin Bar (看涨)")
    if patterns_15m.get("pin_bar_bearish_c2") or patterns_15m.get("pin_bar_bearish_c3"):
        short_score += 8
        reasons_short.append("上影Pin Bar (看跌)")

    if patterns_15m.get("fake_break_below"):
        long_score += 10
        reasons_long.append("假跌破支撑后收回")
    if patterns_15m.get("fake_break_above"):
        short_score += 10
        reasons_short.append("假突破阻力后回落")

    # ── 量能 ──
    vol_ratio = vol_analysis["volume_ratio"]
    if vol_analysis["signal"] == "volume_spike":
        if patterns_15m.get("bullish_engulfing") or patterns_5m.get("bullish_engulfing"):
            long_score += 8
            reasons_long.append(f"放量配合 (x{vol_ratio:.1f})")
        elif patterns_15m.get("bearish_engulfing") or patterns_5m.get("bearish_engulfing"):
            short_score += 8
            reasons_short.append(f"放量配合 (x{vol_ratio:.1f})")
        else:
            warnings.append(f"异常放量 (x{vol_ratio:.1f})，注意变盘")
    elif vol_analysis["signal"] == "volume_shrink":
        if vol_ratio < 0.4:
            # 极度缩量 — 强力惩罚反向
            if long_score > short_score and struct_trend == "bullish":
                long_score -= 8
                reasons_long.append(f"极度缩量上涨 (x{vol_ratio:.1f}), 动能不足")
                warnings.append(f"极度缩量上涨，警惕虚假突破")
            elif short_score > long_score and struct_trend == "bearish":
                short_score -= 8
                reasons_short.append(f"极度缩量下跌 (x{vol_ratio:.1f}), 抛压不足")
        else:
            if long_score > short_score:
                warnings.append("缩量回调，可能是良性调整")
            elif short_score > long_score:
                warnings.append("缩量反弹，力度存疑")

    # ── 资金费率 ──
    if funding["signal"] == "long_crowded":
        short_score += 8
        reasons_short.append(f"多头拥挤 (资金费率{funding['funding_rate']:+.6f})")
        warnings.append("资金费率偏高，多头拥挤，主力可能砸盘")
    elif funding["signal"] == "short_crowded":
        long_score += 8
        reasons_long.append(f"空头拥挤 (资金费率{funding['funding_rate']:+.6f})")
        warnings.append("资金费率偏低，空头拥挤，小心轧空")

    # ── 24h涨跌幅 ──
    if change_24h > 15:
        short_score += 10
        reasons_short.append(f"24h涨幅过大 ({change_24h:+.1f}%)")
        warnings.append("24h涨幅超过15%，追高风险极大")
    elif change_24h < -15:
        long_score += 10
        reasons_long.append(f"24h跌幅过大 ({change_24h:+.1f}%)")
        warnings.append("24h跌幅超过15%，抄底需谨慎")
    elif 5 <= change_24h <= 15:
        short_score += 5
        reasons_short.append(f"24h涨幅适中 ({change_24h:+.1f}%)")
    elif -15 <= change_24h <= -5:
        long_score += 5
        reasons_long.append(f"24h跌幅适中 ({change_24h:+.1f}%)")

    # ── MACD ──
    if macd_data["hist_trend"] == "rising":
        long_score += 8
        reasons_long.append("MACD柱状图上升")
    elif macd_data["hist_trend"] == "falling":
        short_score += 8
        reasons_short.append("MACD柱状图下降")

    # ── MACD与价格背离检测 ──
    if len(closes_15m) >= 60 and len(macd_data["macd_line_series"]) >= 5:
        recent_macd = macd_data["macd_line_series"]
        price_5ago = closes_15m[-5]
        price_now = closes_15m[-1]
        macd_5ago = recent_macd[0]
        macd_now = recent_macd[-1]
        # 顶背离：价格新高，MACD未新高
        if price_now > price_5ago * 1.005 and macd_now < macd_5ago * 0.995:
            short_score += 10
            reasons_short.append("MACD顶背离")
            warnings.append("MACD顶背离，上涨动能衰减")
        # 底背离：价格新低，MACD未新低
        if price_now < price_5ago * 0.995 and macd_now > macd_5ago * 1.005:
            long_score += 10
            reasons_long.append("MACD底背离")
            warnings.append("MACD底背离，下跌动能衰减")

    # ── 多时间周期共振 ──
    if tf_aligned:
        if tf_dominant == "bull":
            long_score += 12
            reasons_long.append("多周期看多共振 (15m/5m/1m)")
        else:
            short_score += 12
            reasons_short.append("多周期看空共振 (15m/5m/1m)")
    else:
        # 只有两个周期对齐
        if trend_15m == trend_5m:
            if trend_15m == "bull":
                long_score += 6
                reasons_long.append("15m+5m看多")
            else:
                short_score += 6
                reasons_short.append("15m+5m看空")

    # ── ATR波动率 ──
    if atr_pct > 5:
        warnings.append(f"高波动 (ATR={atr_pct:.1f}%)，止损需放宽")
    elif atr_pct < 1:
        warnings.append(f"低波动 (ATR={atr_pct:.1f}%)，注意突破")

    # ── 24h区间百分位 —— 追高/捡回调识别 ──
    high_24h = float(ticker.get("highPrice", 0))
    low_24h = float(ticker.get("lowPrice", 0))
    range_24h = high_24h - low_24h
    range_pct = ((price - low_24h) / range_24h * 100) if range_24h > 0 else 50.0

    if range_pct <= 30:
        # 低位——加分但视趋势而定
        if struct_trend in ("bullish", "neutral"):
            long_score += 12
            reasons_long.append(f"价格在24h区间低位 ({range_pct:.0f}%), 回调较充分")
        else:
            short_score += 8
            reasons_short.append(f"区间低位({range_pct:.0f}%, 趋势偏空) 可能是下跌中继")
    elif range_pct > 85:
        # 极高位——无论趋势如何都扣做多分，只是扣分幅度不同
        if struct_trend in ("bearish", "neutral"):
            short_score += 15
            reasons_short.append(f"价格在24h区间极高位 ({range_pct:.0f}%), 追高风险极大")
        else:
            long_score -= 12
            warnings.append(f"区间极高位({range_pct:.0f}%), 等回调再入场")
    elif range_pct > 70:
        # 高位
        if struct_trend in ("bearish", "neutral"):
            short_score += 10
            reasons_short.append(f"价格在24h区间高位 ({range_pct:.0f}%), 反抽阻力")
        else:
            long_score -= 6
            short_score += 4
            reasons_short.append(f"24h区间高位 ({range_pct:.0f}%), 入场偏后")
            warnings.append(f"区间高位({range_pct:.0f}%), 等回调盈亏比更好")

    # ── 入场盈亏比（基于S/R远近） ──
    rr_long = None
    s = sr.get("support")
    r = sr.get("resistance")
    sd = sr.get("sup_dist_pct")
    rd = sr.get("res_dist_pct")

    if s is not None and r is not None and sd is not None and rd is not None:
        rr_long = rd / sd if sd > 0 else 0
        rr_short = sd / rd if rd > 0 else 0
        if rr_long >= 2.0:
            long_score += 8
            reasons_long.append(f"入场盈亏比有利 (1:{rr_long:.1f})")
        elif rr_long < 1.0:
            short_score += 6
            reasons_short.append(f"做多盈亏比差 (1:{rr_long:.1f}), 上行空间不足")
        if rr_short >= 2.0:
            short_score += 8
            reasons_short.append(f"做空盈亏比有利 (1:{rr_short:.1f})")
        elif rr_short < 1.0:
            long_score += 6
            reasons_long.append(f"做空盈亏比差 (1:{rr_short:.1f}), 下行空间不足")
    elif s is not None and sd is not None and sd < 1.5:
        long_score += 6
        reasons_long.append(f"支撑位很近 (-{sd:.1f}%), 下行空间有限")
    elif r is not None and rd is not None and rd < 1.5:
        short_score += 6
        reasons_short.append(f"阻力位很近 (+{rd:.1f}%), 上行空间有限")

    # 备选：用24h高/低做S/R（当无摆动级别S/R时）
    sup_dist_pct_24h = None
    res_dist_pct_24h = None
    rr_from_24h = None
    if s is None and low_24h > 0:
        sup_dist_pct_24h = (price - low_24h) / price * 100 if price > low_24h else 0
        if sup_dist_pct_24h < 3:
            long_score += 6
            reasons_long.append(f"靠近24h低点 ({sup_dist_pct_24h:.1f}%), 短期支撑")
    if r is None and high_24h > price:
        res_dist_pct_24h = (high_24h - price) / price * 100
        
    # ── 实际入场质量检查：到24h高的空间 vs 到24h低/止损的空间 ──
    # 即使有摆动S/R，也用24h高做补充检查
    if high_24h > price:
        up_to_24h_high = (high_24h - price) / price * 100
        # 下行参考：优先用止损幅度，没有则用24h低
        if s is not None and sd is not None:
            down_ref = sd  # 到摆动支撑的距离
        else:
            down_ref = sup_dist_pct_24h if sup_dist_pct_24h is not None else (price - low_24h) / price * 100 if low_24h > 0 else 5.0
        
        if down_ref > 0:
            practical_rr = up_to_24h_high / down_ref
            if practical_rr >= 2.0:
                long_score += 8
                reasons_long.append(f"入场盈亏比有利 (到24h高: +{up_to_24h_high:.1f}% / 到支撑: -{down_ref:.1f}%)")
            elif practical_rr < 1.0 and long_score > short_score:
                # 上行空间 < 下行空间，做多不划算
                long_score -= 10
                short_score += 8
                reasons_short.append(f"做多盈亏比差 (实际1:{practical_rr:.1f}), 上行空间不足")
                warnings.append(f"实际盈亏比差 (1:{practical_rr:.1f}), 入场不划算")

    if sup_dist_pct_24h is not None and sup_dist_pct_24h > 0 and res_dist_pct_24h is not None:
        rr_from_24h = res_dist_pct_24h / sup_dist_pct_24h
        if rr_from_24h >= 2.0:
            long_score += 4
            reasons_long.append(f"24h区间盈亏比有利 (1:{rr_from_24h:.1f})")
        elif rr_from_24h < 1.0:
            short_score += 4
            reasons_short.append(f"24h区间盈亏比差 (1:{rr_from_24h:.1f}), 上行空间不足")

    # ── 计算概率（基于得分差） ──
    # 用 sigmoid-like 将得分差映射到 0~100 概率
    # 净得分差越大，概率越极端，避免被对方分数稀释
    max_possible = 140  # 理论最大单边得分（各条件合计上限）
    net_score = long_score - short_score
    # 映射：得分差 → 概率，差=0时50%，差=70分左右→约75%/25%
    long_prob_raw = 50.0 + (net_score / max_possible) * 50.0
    long_prob = round(max(5, min(95, long_prob_raw)), 1)
    short_prob = round(100 - long_prob, 1)

    # ── 信号等级 A/B/C/D ──
    # A=强信号(>75%+共振), B=中等信号(>65%), C=弱信号(>58%), D=无信号
    signal_strength = max(long_prob, short_prob)
    dominant_score = max(long_score, short_score)

    if signal_strength >= 72 and tf_aligned and dominant_score >= 30:
        grade = "A"
    elif signal_strength >= 65 and dominant_score >= 20:
        grade = "B"
    elif signal_strength >= 58:
        grade = "C"
    else:
        grade = "D"

    return {
        "signal_grade": grade,
        "long_probability": long_prob,
        "short_probability": short_prob,
        "long_score": long_score,
        "short_score": short_score,
        "reasons_long": reasons_long,
        "reasons_short": reasons_short,
        "warnings": warnings,
        "rsi": rsi_val,
        "ema20": ema20_15m,
        "ema50": ema50_15m,
        "ema200": ema200_15m,
        "atr_pct": atr_pct,
        "fib_nearest": fib_check["nearest"] if fib_check["touch"] else None,
        "structure": struct_trend,
        "volume_ratio": vol_ratio,
        "change_24h": change_24h,
        "funding_rate": funding["funding_rate"],
        "range_percentile": round(range_pct, 1),
        "rr_metric": round(rr_long, 1) if rr_long is not None else "-",
        "support": sr["support"],
        "resistance": sr["resistance"],
        "sup_dist_pct": sr["sup_dist_pct"],
        "res_dist_pct": sr["res_dist_pct"],
        "tf_aligned": tf_aligned,
        "tf_dominant": tf_dominant,
        "macd_hist_trend": macd_data["hist_trend"],
    }


# ─── 9. 仓位与风控建议 ──────────────────────────────────────────────

def risk_recommendation(
    price: float,
    score_result: dict,
    account_balance: float = 1000,
    risk_per_trade_pct: float = 2.0,
) -> dict:
    """
    基于评分和ATR计算仓位、止盈止损。
    risk_per_trade_pct: 每单承担的本金风险百分比（默认2%）
    """
    atr_pct = score_result["atr_pct"]
    long_prob = score_result["long_probability"]
    short_prob = score_result["short_probability"]

    # ── 方向判断 ──
    # 新概率已经是基于净得分差计算，60%即表示一方占明显优势
    if long_prob > short_prob and long_prob >= 65:
        direction = "LONG"
        confidence = long_prob
    elif short_prob > long_prob and short_prob >= 65:
        direction = "SHORT"
        confidence = short_prob
    else:
        direction = "NEUTRAL"
        confidence = max(long_prob, short_prob)

    # ── 止损 ──
    # 基础止损 = 1.5倍ATR，根据评分调整
    base_sl_pct = max(atr_pct * 1.5, 0.5)  # 至少0.5%
    # 评分越高，止损可以越紧；评分低说明不确定性大，需要放宽
    score_confidence = max(long_prob, short_prob)
    if score_confidence >= 75:
        sl_multiplier = 1.2
    elif score_confidence >= 65:
        sl_multiplier = 1.5
    else:
        sl_multiplier = 2.0

    sl_pct = base_sl_pct * sl_multiplier

    if direction == "LONG":
        stop_loss = price * (1 - sl_pct / 100)
    elif direction == "SHORT":
        stop_loss = price * (1 + sl_pct / 100)
    else:
        stop_loss = None

    # ── 止盈（1:2 盈亏比保底，评分高可提高） ──
    if score_confidence >= 75:
        rr_ratio = 2.5
    elif score_confidence >= 65:
        rr_ratio = 2.0
    else:
        rr_ratio = 1.5

    tp_pct = sl_pct * rr_ratio

    if direction == "LONG":
        take_profit = price * (1 + tp_pct / 100)
    elif direction == "SHORT":
        take_profit = price * (1 - tp_pct / 100)
    else:
        take_profit = None

    # ── 仓位计算 ──
    # 每单风险金额 = 本金 * risk_per_trade_pct%
    risk_amount = account_balance * risk_per_trade_pct / 100
    # 仓位 = 风险金额 / 止损百分比
    position_size = risk_amount / (sl_pct / 100) if sl_pct > 0 else 0
    # 仓位占本金比例
    position_pct = position_size / account_balance * 100 if account_balance > 0 else 0

    # 置信度较低时减仓
    if score_confidence < 75:
        position_pct *= 0.6  # 75%以下仓位减半

    # ── 杠杆建议 ──
    # 原则：波动大=低杠杆，波动小=高杠杆
    # ATR < 1% → 5-10x | 1-3% → 3-5x | 3-5% → 2-3x | > 5% → 1-2x
    if atr_pct < 0.5:
        suggested_leverage = 10
    elif atr_pct < 1.0:
        suggested_leverage = 8
    elif atr_pct < 2.0:
        suggested_leverage = 5
    elif atr_pct < 3.5:
        suggested_leverage = 3
    elif atr_pct < 5.0:
        suggested_leverage = 2
    else:
        suggested_leverage = 1

    # 置信度低时降杠杆
    if score_confidence < 60:
        suggested_leverage = max(1, suggested_leverage - 2)

    # 取整后的保证金比例
    capped_pct = round(min(position_pct, 50), 1)
    # 杠杆下的实际名义仓位
    margin_used = account_balance * (capped_pct / 100) if capped_pct > 0 else 0
    notional_value = margin_used * suggested_leverage

    return {
        "direction": direction,
        "confidence": round(confidence, 1),
        "entry_price": price,
        "stop_loss": round(stop_loss, 8) if stop_loss else None,
        "take_profit": round(take_profit, 8) if take_profit else None,
        "sl_pct": round(sl_pct, 2),
        "tp_pct": round(tp_pct, 2),
        "rr_ratio": rr_ratio,
        "leverage": suggested_leverage,
        "position_pct": capped_pct,
        "position_value": round(min(position_size, account_balance * 0.5), 2),
        "notional_value": round(notional_value, 2),
        "risk_amount": round(risk_amount, 2),
    }


# ─── 10. 输出格式化 ──────────────────────────────────────────────────

def format_output(
    symbol: str,
    score: dict,
    risk: dict,
    price: float,
    ticker: dict,
    brief: bool = False,
) -> str:
    """人类可读的分析报告"""
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

    # ── 信号等级标签 ──
    grade_emoji = {"A": "🅰️ 强信号", "B": "🅱️ 中等", "C": "©️ 弱信号", "D": "⚪ 无信号"}
    grade_label = grade_emoji.get(score["signal_grade"], "")

    lines = []
    lines.append(f"{'='*56}")
    lines.append(f"  🦐 币安信号雷达  |  {symbol.upper()}  |  {now}")
    lines.append(f"  信号等级: {grade_label}")
    lines.append(f"{'='*56}")

    # ── 当前价格 ──
    change_24h = float(ticker.get("priceChangePercent", 0))
    change_str = f"{change_24h:+.2f}%"
    high_24h = float(ticker.get("highPrice", 0))
    low_24h = float(ticker.get("lowPrice", 0))
    vol_24h = float(ticker.get("quoteVolume", 0))

    lines.append(f"  💰 当前价: ${price:,.6f}  (24h: {change_str})")
    if not brief:
        lines.append(f"     24h 高: ${high_24h:,.6f}  低: ${low_24h:,.6f}  量: ${vol_24h:,.0f}")
    lines.append("")

    # ── 多空概率 ──
    long_bar = "■" * int(score["long_probability"] / 5)
    short_bar = "■" * int(score["short_probability"] / 5)
    lines.append(f"  📊 多空概率")
    lines.append(f"     🟢 做多: {score['long_probability']}%  {long_bar}")
    lines.append(f"     🔴 做空: {score['short_probability']}%  {short_bar}")
    lines.append("")

    # ── 技术概览 ──
    tf_tag = " ✅" if score.get("tf_aligned") else " ❌"
    lines.append(f"  📈 技术指标")
    lines.append(f"     RSI(14): {score['rsi']:.1f}  |  EMA20: ${score['ema20']:,.4f}  |  EMA50: ${score['ema50']:,.4f}")
    lines.append(f"     结构: {score['structure']}  |  ATR: {score['atr_pct']:.2f}%  |  MACD: {score['macd_hist_trend']}")
    lines.append(f"     量比: x{score['volume_ratio']:.1f}  |  资金费率: {score['funding_rate']:+.6f}  |  共振:{tf_tag}")
    if score["fib_nearest"]:
        lines.append(f"     Fib靠近: {score['fib_nearest']}")
    lines.append(f"     区间百分位: {score.get('range_percentile', 50):.0f}%  |  S/R盈亏比: {score.get('rr_metric', '-'):>6}")
    if not brief:
        s = score.get("support")
        r = score.get("resistance")
        sd = score.get("sup_dist_pct")
        rd = score.get("res_dist_pct")
        s_str = f"支撑 ${s:,.4f} (-{sd}%)" if s is not None else "支撑 —"
        # 24h高/低作为备用S/R
        h24 = float(ticker.get("highPrice", 0))
        l24 = float(ticker.get("lowPrice", 0))
        r_str = f"阻力 ${r:,.4f} (+{rd}%)" if r is not None else (
            f"阻力 ${h24:,.4f} (+{(h24-price)/price*100:.1f}%, 24h高)" if h24 > price else "阻力 —"
        )
        lines.append(f"     S/R: {s_str}  |  {r_str}")
    lines.append("")

    # ── 评分理由 ──
    if score["reasons_long"]:
        lines.append(f"  🟢 做多理由 ({score['long_score']}分)")
        for r in score["reasons_long"]:
            lines.append(f"     ✓ {r}")
        lines.append("")

    if score["reasons_short"]:
        lines.append(f"  🔴 做空理由 ({score['short_score']}分)")
        for r in score["reasons_short"]:
            lines.append(f"     ✓ {r}")
        lines.append("")

    if not brief:
        # ── 风险提示 ──
        if score["warnings"]:
            lines.append(f"  ⚠️ 风险提示")
            for w in score["warnings"]:
                lines.append(f"     · {w}")
            lines.append("")

    # ── 操作建议 ──
    lines.append(f"  {'='*52}")
    lines.append(f"  📋 操作建议  [等级 {score['signal_grade']}]")
    lines.append(f"  {'='*52}")

    dir_emoji = {"LONG": "🟢 做多", "SHORT": "🔴 做空", "NEUTRAL": "⚪ 观望"}

    if risk["direction"] == "NEUTRAL" or risk["confidence"] < 60:
        lines.append(f"     建议: ⚪ 观望（信号不明确，信噪比过低）")
        lines.append(f"     置信度: {risk['confidence']}%")
        lines.append("")
        lines.append(f"     💡 等待以下条件改善后再入场：")
        lines.append(f"        · 趋势更明确（EMA排列清晰）")
        lines.append(f"        · 结构更完整（HH/HL或LH/LL成立）")
        lines.append(f"        · K线形态确认")
        lines.append(f"        · 量能配合")
    else:
        # 止损永远是亏损（负号），止盈永远是盈利（正号）
        sl_sign = f"-{risk['sl_pct']:.2f}%"
        tp_sign = f"+{risk['tp_pct']:.2f}%"

        lines.append(f"     方向: {dir_emoji.get(risk['direction'], risk['direction'])}")
        lines.append(f"     置信度: {risk['confidence']}%")
        lines.append(f"     入场价: ${risk['entry_price']:,.6f}")
        lines.append(f"     止损价: ${risk['stop_loss']:,.6f}  ({sl_sign})")
        lines.append(f"     止盈价: ${risk['take_profit']:,.6f}  ({tp_sign})")
        lines.append(f"     盈亏比: 1:{risk['rr_ratio']}")
        lines.append(f"     建议杠杆: {risk['leverage']}x  (保证金{risk['position_pct']}%)")
        lines.append(f"     名义仓位: ${risk['notional_value']:,.2f}")
        lines.append(f"     每单最大亏损: ${risk['risk_amount']:.2f}  (本金2.0%)")

        # 安全提示
        if risk["leverage"] >= 8:
            lines.append("")
            lines.append(f"     ⚠️ 高杠杆 ({risk['leverage']}x)，注意波动风险")
        if risk["position_pct"] > 20:
            lines.append("")
            lines.append(f"     ⚠️ 保证金占用较高，建议分批入场")

    lines.append("")
    lines.append(f"  {'─'*52}")
    lines.append(f"  💡 严格执行: 入场前检查所有A+条件")
    lines.append(f"     没有信号 = 空仓")
    lines.append(f"  {'='*52}")

    return "\n".join(lines)


# ─── Main ──────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("用法: python3 binance_scalping_analyzer.py <币种> [本金] [--brief]")
        print("示例: python3 binance_scalping_analyzer.py BTCUSDT")
        print("      python3 binance_scalping_analyzer.py BTC 5000 --brief")
        sys.exit(1)

    # 解析参数
    brief = "--brief" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    raw = args[0].upper().strip()
    # 自动补全 USDT
    symbol = raw if raw.endswith("USDT") else raw + "USDT"

    # 账户本金（从参数或默认）
    balance = 1000.0
    if len(args) >= 2:
        balance = float(args[1])

    try:
        # 获取实时价格与24h数据
        if not brief:
            print(f"🔄 正在获取 {symbol} 数据...")
        ticker = fetch_ticker(symbol)
        price = float(ticker["lastPrice"])
        if not brief:
            print(f"   ✓ 当前价格: ${price:,.6f}")

        # 获取多周期K线
        if not brief:
            print(f"   ✓ 获取15m K线...")
        klines_15m = fetch_klines(symbol, "15m", 200)
        if not brief:
            print(f"   ✓ 获取5m K线...")
        klines_5m = fetch_klines(symbol, "5m", 100)
        if not brief:
            print(f"   ✓ 获取1m K线...")
        klines_1m = fetch_klines(symbol, "1m", 60)

        # 资金费率
        if not brief:
            print(f"   ✓ 获取资金费率...")
        funding = fetch_funding_rate(symbol)

        # 评分
        if not brief:
            print(f"   🧮 运行评分引擎...")
        score = score_system(price, klines_15m, klines_5m, klines_1m, ticker, funding)

        # 风控建议
        risk = risk_recommendation(price, score, balance)

        # 输出
        output = format_output(symbol, score, risk, price, ticker, brief)
        print("\n" + output)

    except (urllib.error.URLError, ConnectionError) as e:
        print(f"❌ 网络错误: {e}")
        print("   已自动重试3次+降级到现货API，请稍后再试")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ 数据解析错误: {e}")
        print("   可能币种不存在或API返回异常")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 未知错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
