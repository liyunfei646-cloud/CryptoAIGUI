"""
futures_data.py — 合约衍生品数据层 + Market Regime 检测 (V2.0 Phase 0)
======================================================================
对应《币安短线交易分析模型 V2.0 优化方案》P0 部分：

  P0-1  接入真实 OI（当前值 + 5m/15m/1h 变化率）
  P0-2  接入 Funding 趋势（币安 funding 每 8h 结算一次，用历史结算序列看趋势）
  P0-3  接入 Taker Buy/Sell（takerlongshortRatio）
  P0-5  Market Regime 检测（4H/1H 趋势骨架 + 波动率 + 区间/突破）

数据源（币安 USDⓈ-M Futures 公开接口，无需鉴权）：
  /fapi/v1/openInterest                   当前 OI（币数量）
  /futures/data/openInterestHist         OI 历史（5m/15m/1h 粒度，sumOpenInterestValue=USDT）
  /fapi/v1/fundingRate                    历史结算资金费率（8h 间隔）
  /futures/data/takerlongshortRatio       Taker 主动买卖量比
  /futures/data/globalLongShortAccountRatio  多空账户数比
  /futures/data/forceOrders               最近强平（仅增量，无历史，尽力而为）

原则：
  - 任何接口失败都优雅降级（None / 空），绝不中断主流程
  - 变化率只基于已发生的数据计算，不引入未来函数
  - Market Regime 是 V2.0 所有因子解释的地基：同一指标在不同环境下含义不同
"""

import json
import time
import ssl
import urllib.request
from typing import Optional

# ─── 配置 ────────────────────────────────────────────────────────────────
BINANCE_FUTURES = "https://fapi.binance.com"
_CACHE_TTL = 30  # 秒 — 超短线场景缓存较短

_DATA_CACHE: dict = {}


# ═══════════════════════════════════════════════════════════════════════
#  基础 HTTP / 缓存
# ═══════════════════════════════════════════════════════════════════════

def _http_get(url: str, timeout: int = 10) -> bytes:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FuturesData/2.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except Exception:
            if attempt < 1:
                time.sleep(0.3)
    return b""


def _http_get_json(url: str, timeout: int = 10):
    """带缓存的 JSON 请求；失败返回 None。"""
    now = time.time()
    cached = _DATA_CACHE.get(url)
    if cached and (now - cached["ts"]) < _CACHE_TTL:
        return cached["data"]
    raw = _http_get(url, timeout)
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    _DATA_CACHE[url] = {"ts": now, "data": data}
    return data


# ═══════════════════════════════════════════════════════════════════════
#  指标工具（独立实现，避免与 analyzer.py 循环依赖）
# ═══════════════════════════════════════════════════════════════════════

def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    if len(values) < period:
        period = max(2, len(values))
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def atr(klines: list[dict], period: int = 14) -> float:
    if len(klines) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(klines)):
        h = klines[i]["high"]
        l = klines[i]["low"]
        pc = klines[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


# ═══════════════════════════════════════════════════════════════════════
#  OI（持仓量）
# ═══════════════════════════════════════════════════════════════════════

def fetch_open_interest(symbol: str) -> Optional[float]:
    """当前 OI（币数量）。失败返回 None。"""
    sym = symbol.upper()
    data = _http_get_json(f"{BINANCE_FUTURES}/fapi/v1/openInterest?symbol={sym}")
    if not data:
        return None
    try:
        return float(data.get("openInterest", 0))
    except Exception:
        return None


def fetch_oi_series(symbol: str, period: str = "5m", limit: int = 24) -> Optional[list[dict]]:
    """
    OI 历史序列（按 sumOpenInterestValue=USDT 价值）。
    period: 5m/15m/1h/4h/1d
    """
    sym = symbol.upper()
    data = _http_get_json(
        f"{BINANCE_FUTURES}/futures/data/openInterestHist?symbol={sym}&period={period}&limit={limit}"
    )
    if not data:
        return None
    out = []
    for d in data:
        try:
            out.append({
                "time": int(d.get("timestamp", 0)),
                "oi": float(d.get("sumOpenInterest", 0)),
                "oi_value": float(d.get("sumOpenInterestValue", 0)),
            })
        except Exception:
            continue
    return out or None


# ═══════════════════════════════════════════════════════════════════════
#  Funding（资金费率）
# ═══════════════════════════════════════════════════════════════════════

def fetch_funding_series(symbol: str, limit: int = 6) -> Optional[list[dict]]:
    """历史结算资金费率（每 8h 一条）。"""
    sym = symbol.upper()
    data = _http_get_json(f"{BINANCE_FUTURES}/fapi/v1/fundingRate?symbol={sym}&limit={limit}")
    if not data:
        return None
    out = []
    for d in data:
        try:
            out.append({
                "time": int(d.get("fundingTime", 0)),
                "rate": float(d.get("fundingRate", 0)),
            })
        except Exception:
            continue
    return out or None


# ═══════════════════════════════════════════════════════════════════════
#  Taker Buy/Sell
# ═══════════════════════════════════════════════════════════════════════

def fetch_taker_series(symbol: str, period: str = "5m", limit: int = 24) -> Optional[list[dict]]:
    """
    Taker 主动买卖（buySellRatio = 主动买/主动卖）。
    period: 5m/15m/30m/1h/2h/4h/6h/12h/1d
    """
    sym = symbol.upper()
    data = _http_get_json(
        f"{BINANCE_FUTURES}/futures/data/takerlongshortRatio?symbol={sym}&period={period}&limit={limit}"
    )
    if not data:
        return None
    out = []
    for d in data:
        try:
            out.append({
                "time": int(d.get("timestamp", 0)),
                "ratio": float(d.get("buySellRatio", 1.0)),
                "buy_vol": float(d.get("buyVol", 0)),
                "sell_vol": float(d.get("sellVol", 0)),
            })
        except Exception:
            continue
    return out or None


def fetch_global_ls_ratio(symbol: str, period: str = "1h") -> Optional[float]:
    """全账户多空持仓账户数比。>1 多头账户多。"""
    sym = symbol.upper()
    data = _http_get_json(
        f"{BINANCE_FUTURES}/futures/data/globalLongShortAccountRatio?symbol={sym}&period={period}&limit=1"
    )
    if not data:
        return None
    try:
        return float(data[0].get("longShortRatio", 1.0))
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
#  Liquidation（强平，尽力而为）
# ═══════════════════════════════════════════════════════════════════════

def fetch_recent_liquidations(symbol: str, window_ms: int = 5 * 60 * 1000) -> dict:
    """
    最近强平统计（币安仅提供增量数据，无历史，限流严格）。
    返回 {long: USDT(爆多), short: USDT(爆空), count: n}
    side=SELL → 强平多单（爆多）；side=BUY → 强平空单（爆空）。
    """
    sym = symbol.upper()
    result = {"long": None, "short": None, "count": 0}
    try:
        data = _http_get_json(f"{BINANCE_FUTURES}/futures/data/forceOrders?symbol={sym}&limit=100")
        if not data:
            return result
        now_ms = time.time() * 1000
        liq_long = 0.0
        liq_short = 0.0
        count = 0
        for f in data:
            t = f.get("time", 0)
            if t and (now_ms - t) <= window_ms:
                try:
                    notional = float(f.get("price", 0)) * float(f.get("origQty", 0))
                except Exception:
                    continue
                if f.get("side") == "SELL":
                    liq_long += notional
                elif f.get("side") == "BUY":
                    liq_short += notional
                count += 1
        result = {
            "long": round(liq_long, 2) if count else None,
            "short": round(liq_short, 2) if count else None,
            "count": count,
        }
    except Exception:
        pass
    return result


# ═══════════════════════════════════════════════════════════════════════
#  聚合上下文（供评分 / 审计使用）
# ═══════════════════════════════════════════════════════════════════════

def build_derivatives_context(symbol: str) -> dict:
    """
    一次性拉取全部衍生品数据并计算变化率/趋势。

    返回字段：
      oi / oi_change_5m / oi_change_15m / oi_change_1h / oi_trend
      funding_rate / funding_series / funding_trend / funding_regime
      taker_ratio / taker_buy_vol / taker_sell_vol / taker_ratio_1h_ago / taker_trend
      global_ls_ratio
      liq_5m_long / liq_5m_short
      errors
    """
    sym = symbol.upper()
    ctx = {
        "oi": None,
        "oi_change_5m": None, "oi_change_15m": None, "oi_change_1h": None,
        "oi_trend": "unknown",
        "funding_rate": None, "funding_level": None, "funding_series": [],
        "funding_trend": "flat", "funding_acceleration": 0.0,
        "funding_regime": "normal",
        "taker_ratio": None, "taker_buy_vol": None, "taker_sell_vol": None,
        "taker_ratio_1h_ago": None, "taker_trend": "neutral",
        "global_ls_ratio": None,
        "liq_5m_long": None, "liq_5m_short": None,
        "errors": {},
    }

    # 1) 当前 OI
    try:
        oi = fetch_open_interest(sym)
        if oi is not None:
            ctx["oi"] = oi
    except Exception as e:
        ctx["errors"]["oi"] = str(e)

    # 2) OI 历史 → 5m/15m/1h 变化率（5m 粒度，24 根 = 2 小时）
    try:
        hist = fetch_oi_series(sym, "5m", 24)
        if hist and len(hist) >= 13:
            vals = [h["oi_value"] for h in hist]
            cur = vals[-1]

            def chg(idx: int):
                i = idx if idx >= 0 else len(vals) + idx
                if 0 <= i < len(vals) and vals[i] > 0:
                    return (cur - vals[i]) / vals[i] * 100
                return None

            ctx["oi_change_5m"] = round(chg(-2), 2) if len(vals) >= 2 else None
            ctx["oi_change_15m"] = round(chg(-4), 2) if len(vals) >= 4 else None
            ctx["oi_change_1h"] = round(chg(-13), 2) if len(vals) >= 13 else None
            chg15 = ctx["oi_change_15m"]
            if chg15 is not None:
                if chg15 > 1.0:
                    ctx["oi_trend"] = "rising"
                elif chg15 < -1.0:
                    ctx["oi_trend"] = "falling"
                else:
                    ctx["oi_trend"] = "flat"
    except Exception as e:
        ctx["errors"]["oi_hist"] = str(e)

    # 3) Funding 历史结算趋势（V2.1: level / trend / acceleration 三要素，文档17节）
    try:
        frs = fetch_funding_series(sym, 8)
        if frs:
            ctx["funding_series"] = frs
            rates = [r["rate"] for r in frs]
            ctx["funding_rate"] = rates[-1]
            ctx["funding_level"] = rates[-1]
            if len(rates) >= 4:
                recent = rates[-4:]
                up = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
                dn = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i - 1])
                if up >= 3:
                    ctx["funding_trend"] = "rising"
                elif dn >= 3:
                    ctx["funding_trend"] = "falling"
                else:
                    ctx["funding_trend"] = "flat"
            # acceleration: 最近两段结算的变化（区分"高且继续升高"与"高但快速下降"）
            if len(rates) >= 3:
                ctx["funding_acceleration"] = rates[-1] - rates[-2]
            elif len(rates) >= 2:
                ctx["funding_acceleration"] = rates[-1] - rates[-2]
            else:
                ctx["funding_acceleration"] = 0.0
            fr = ctx["funding_rate"]
            if fr is not None:
                if fr >= 0.0005:
                    ctx["funding_regime"] = "extreme_long"
                elif fr >= 0.0001:
                    ctx["funding_regime"] = "long_crowded"
                elif fr <= -0.0005:
                    ctx["funding_regime"] = "extreme_short"
                elif fr <= -0.0001:
                    ctx["funding_regime"] = "short_crowded"
    except Exception as e:
        ctx["errors"]["funding"] = str(e)

    # 4) Taker Buy/Sell
    try:
        tk = fetch_taker_series(sym, "5m", 24)
        if tk and len(tk) >= 2:
            ctx["taker_ratio"] = tk[-1]["ratio"]
            ctx["taker_buy_vol"] = tk[-1]["buy_vol"]
            ctx["taker_sell_vol"] = tk[-1]["sell_vol"]
            idx_1h = min(12, len(tk) - 1)
            ctx["taker_ratio_1h_ago"] = tk[-1 - idx_1h]["ratio"]
            r_now = ctx["taker_ratio"]
            r_ago = ctx["taker_ratio_1h_ago"]
            if r_now is not None and r_ago is not None:
                if r_now >= 1.1 and r_now > r_ago * 1.05:
                    ctx["taker_trend"] = "buy_dominant"
                elif r_now <= 0.9 and r_now < r_ago * 0.95:
                    ctx["taker_trend"] = "sell_dominant"
                else:
                    ctx["taker_trend"] = "neutral"
    except Exception as e:
        ctx["errors"]["taker"] = str(e)

    # 5) 全账户多空比
    try:
        ls = fetch_global_ls_ratio(sym, "1h")
        if ls is not None:
            ctx["global_ls_ratio"] = ls
    except Exception as e:
        ctx["errors"]["global_ls"] = str(e)

    # 6) 最近强平（尽力而为）
    try:
        liq = fetch_recent_liquidations(sym)
        ctx["liq_5m_long"] = liq["long"]
        ctx["liq_5m_short"] = liq["short"]
    except Exception as e:
        ctx["errors"]["liq"] = str(e)

    return ctx


# ═══════════════════════════════════════════════════════════════════════
#  Market Regime 检测（V2.0 核心层）
# ═══════════════════════════════════════════════════════════════════════

def detect_market_regime(klines_4h: list[dict], klines_1h: list[dict],
                         vol_threshold_pct: float = 3.0) -> dict:
    """
    输入【已收盘】的 4H / 1H K线，输出 Market Regime。

    Regime 定义：
      TREND_UP     4H+1H 同向多头趋势
      TREND_DOWN   4H+1H 同向空头趋势
      RANGE        价格围绕 4H EMA50 震荡 + 均线纠缠
      BREAKOUT     向上突破 20 根 4H 区间高点
      BREAKDOWN    向下突破 20 根 4H 区间低点
      CHAOS        周期矛盾 / 数据不足 / 高波动无方向
    附加：high_volatility 标志（4H ATR% 超阈值）
    """
    default = {"regime": "CHAOS", "regime_name": "CHAOS",
               "regime_strength": 0.0, "volatility_state": "UNKNOWN",
               "trend_4h": "FLAT", "trend_1h": "FLAT",
               "atr_pct": 0.0, "high_volatility": False,
               "range_high": None, "range_low": None,
               "ema50_4h": None, "ema200_4h": None, "reasons": ["数据不足"]}
    if not klines_4h or not klines_1h or len(klines_4h) < 60 or len(klines_1h) < 60:
        return default

    c4 = [k["close"] for k in klines_4h]
    h4 = [k["high"] for k in klines_4h]
    l4 = [k["low"] for k in klines_4h]
    price = c4[-1]
    if price <= 0:
        return default

    ema50_4h = ema_series(c4, 50)[-1]
    ema200_4h = ema_series(c4, 200)[-1]
    atr4 = atr(klines_4h, 14)
    atr_pct = atr4 / price * 100

    # 4H 趋势骨架（均线纠缠 → FLAT）
    ema_gap = abs(ema50_4h - ema200_4h) / price * 100
    if ema_gap < 0.5:
        trend_4h = "FLAT"
    elif ema50_4h > ema200_4h:
        trend_4h = "UP"
    else:
        trend_4h = "DOWN"

    # 1H 趋势
    c1 = [k["close"] for k in klines_1h]
    ema50_1h = ema_series(c1, 50)[-1]
    ema200_1h = ema_series(c1, 200)[-1]
    gap1 = abs(ema50_1h - ema200_1h) / c1[-1] * 100 if c1[-1] > 0 else 0
    if gap1 < 0.3:
        trend_1h = "FLAT"
    elif ema50_1h > ema200_1h:
        trend_1h = "UP"
    else:
        trend_1h = "DOWN"

    # 区间与突破（不含最后一根，避免自我实现的未来函数）
    win = 20
    if len(h4) > win + 1:
        range_high = max(h4[-win - 1:-1])
        range_low = min(l4[-win - 1:-1])
    else:
        range_high = max(h4)
        range_low = min(l4)
    breakout_up = price > range_high
    breakout_dn = price < range_low

    # 震荡判定：最近 20 根收盘价在 EMA50 ± 1.5*ATR 内
    recent = c4[-win:]
    in_range = all(abs(c - ema50_4h) <= 1.5 * atr4 for c in recent) if atr4 > 0 else False

    high_vol = atr_pct > vol_threshold_pct
    # V2.1: 波动率状态（NORMAL / HIGH / EXTREME）
    if atr_pct > vol_threshold_pct * 2:
        volatility_state = "EXTREME"
    elif atr_pct > vol_threshold_pct:
        volatility_state = "HIGH"
    else:
        volatility_state = "NORMAL"
    reasons = []

    if breakout_up:
        regime = "BREAKOUT"
        reasons.append(f"突破20根4H区间高点 ${range_high:,.4f}")
    elif breakout_dn:
        regime = "BREAKDOWN"
        reasons.append(f"跌破20根4H区间低点 ${range_low:,.4f}")
    elif in_range and trend_4h == "FLAT":
        regime = "RANGE"
        reasons.append("价格围绕4H EMA50震荡，均线纠缠")
    elif trend_4h == "UP" and trend_1h in ("UP", "FLAT"):
        regime = "TREND_UP"
        reasons.append(f"4H多头+1H{trend_1h}，趋势向上")
    elif trend_4h == "DOWN" and trend_1h in ("DOWN", "FLAT"):
        regime = "TREND_DOWN"
        reasons.append(f"4H空头+1H{trend_1h}，趋势向下")
    elif trend_4h in ("UP", "DOWN") and trend_1h in ("UP", "DOWN") and trend_4h != trend_1h:
        regime = "CHAOS"
        reasons.append(f"周期矛盾 (4H={trend_4h} vs 1H={trend_1h})")
    elif high_vol:
        regime = "CHAOS"
        reasons.append(f"高波动无方向 (ATR {atr_pct:.1f}%)")
    else:
        regime = "RANGE"
        reasons.append("默认震荡判定")

    if high_vol:
        reasons.append(f"高波动 (4H ATR {atr_pct:.1f}%)")

    # V2.1: regime_strength（0~1，趋势/突破的明确程度）
    if regime in ("TREND_UP", "TREND_DOWN"):
        regime_strength = min(1.0, ema_gap / 1.5)
    elif regime in ("BREAKOUT", "BREAKDOWN"):
        dist = abs(price - (range_high if regime == "BREAKOUT" else range_low))
        regime_strength = min(1.0, dist / (2 * atr4)) if atr4 > 0 else 0.6
    elif regime == "RANGE":
        regime_strength = 0.3
    else:
        regime_strength = 0.2

    return {
        "regime": regime,
        "regime_name": regime,  # V2.1 别名（文档18节）
        "regime_strength": round(regime_strength, 2),
        "volatility_state": volatility_state,
        "trend_4h": trend_4h,
        "trend_1h": trend_1h,
        "atr_pct": round(atr_pct, 2),
        "high_volatility": high_vol,
        "range_high": range_high,
        "range_low": range_low,
        "ema50_4h": ema50_4h,
        "ema200_4h": ema200_4h,
        "reasons": reasons,
    }


# ═══════════════════════════════════════════════════════════════════════
#  快速测试
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    def test(sym: str):
        print(f"\n{'=' * 60}")
        print(f"  Derivatives Context — {sym}")
        print(f"{'=' * 60}")
        ctx = build_derivatives_context(sym)
        for k, v in ctx.items():
            if k != "funding_series":
                print(f"  {k:>22}: {v}")
        print(f"  funding_series: {ctx['funding_series']}")

    if len(sys.argv) > 1:
        test(sys.argv[1])
    else:
        test("BTCUSDT")
        test("SOLUSDT")
