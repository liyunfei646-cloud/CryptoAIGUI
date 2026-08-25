#!/usr/bin/env python3
"""
🐚 4h做空信号检查 - QQ机器人专用
========================================
基于 2026-07-31 回测验证结论：
- 15m 信号≈随机（48-51%）→ 废弃
- 4h 做空因子命中 65-83% → 唯一可交易信号
- 山寨币做多因子反向 → 禁止做多

用法:
  python3 coin_analysis_4h.py SPCX
  python3 coin_analysis_4h.py SPCXUSDT
"""
import sys, os, importlib.util, datetime, json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


analyzer = _load_module("analyzer", os.path.join(SCRIPT_DIR, "analyzer.py"))
# V2.0 组件：Market Regime 检测（futures_data.py，与 GUI 共用）
try:
    futures_data = _load_module("futures_data", os.path.join(SCRIPT_DIR, "futures_data.py"))
    HAS_REGIME = True
except Exception:
    futures_data = None
    HAS_REGIME = False

INTERVAL = "4h"
MIN_FACTORS = 4
MAX_CHARS = 1600

# 已验证的山寨币（禁止做多）— 其他币默认按"非山寨"处理
ALTS = {"SPCX", "AERGO", "BANK", "XNY", "ZEREBRO", "CHR", "NIL", "DYDX", "TON",
        "ONDO", "PSG", "LAYER", "SUI", "OSMO", "CATI", "CHIP", "HMSTR", "KERNEL"}


def leverage_for_atr(atr_pct, confidence):
    if atr_pct < 0.5: lev = 10
    elif atr_pct < 1.0: lev = 8
    elif atr_pct < 2.0: lev = 5
    elif atr_pct < 3.5: lev = 3
    elif atr_pct < 5.0: lev = 2
    else: lev = 1
    if confidence < 60:
        lev = max(1, lev - 2)
    return lev


def _trend_cn(t):
    return {"bearish": "下跌", "bullish": "上涨", "bearish_div": "高点抬高低点未抬(空头背离)",
            "bullish_div": "低点降低高点未降(多头衰竭)", "neutral": "震荡/无清晰结构"}.get(t, t)


def _vol_cn(s):
    return {"volume_spike": "放量", "volume_shrink": "缩量", "normal": "平量"}.get(s, s)


def fetch_oi_trend(symbol: str, period: str = "4h", limit: int = 9):
    """拉取币安合约持仓量历史序列(由旧到新)，失败返回 None"""
    sym = symbol.upper()
    url = f"{analyzer.BINANCE_FUTURES}/futures/data/openInterestHist?symbol={sym}&period={period}&limit={limit}"
    try:
        raw = json.loads(analyzer._http_get(url).decode("utf-8"))
        if not raw:
            return None
        return [float(d["sumOpenInterest"]) for d in raw]
    except Exception:
        return None


def detect_regime(symbol: str):
    """V2.0 Market Regime 检测（4h+1h 双层），失败返回 None。"""
    if not HAS_REGIME:
        return None
    try:
        k4h = analyzer.fetch_klines(symbol, "4h", 200)
        k1h = analyzer.fetch_klines(symbol, "1h", 200)
        if len(k4h) < 60 or len(k1h) < 60:
            return None
        reg = futures_data.detect_market_regime(k4h, k1h)
        return {"regime": reg.get("regime"), "atr_pct": reg.get("atr_pct"),
                "high_volatility": reg.get("high_volatility")}
    except Exception:
        return None


def structural_sl_tp(price: float, atr_pct: float, klines: list, swing_highs: list, swing_lows: list):
    """V2.0 结构止损 + 动态止盈（SHORT 版，文档17节逻辑）。

    SL  = 最近结构高点×1.001 缓冲（与 ATR×1.0 取大，上限 ATR×3，下限 0.5%）
    TP1 = 最近结构支撑（下一流动性目标）
    TP2 = 24h 低点（再下一目标）
    返回 dict；swing 不足时自动降级为 ATR 止损。
    """
    # SL：最近高于现价的结构高点
    swing_high_recent = None
    for sh in reversed(swing_highs):
        if sh[1] > price:
            swing_high_recent = sh[1]
            break
    if swing_high_recent:
        struct_dist = (swing_high_recent * 1.001 - price) / price * 100
        sl_pct = max(struct_dist, atr_pct * 1.0)
        sl_basis = "结构高点"
    else:
        sl_pct = atr_pct * 1.5
        sl_basis = "ATR"
    sl_pct = min(sl_pct, atr_pct * 3.0)
    sl_pct = max(sl_pct, 0.5)

    # TP1：最近低于现价的结构支撑
    supports = sorted(set(l[1] for l in swing_lows))
    nearest_support = None
    for s in supports:
        if s < price:
            nearest_support = s
            break
    if nearest_support and nearest_support > 0:
        tp1_pct = (price - nearest_support) / price * 100
    else:
        tp1_pct = sl_pct * 2.0

    # TP2：24h 低点（4h×6）
    window = klines[-6:]
    low_24h = min(k["low"] for k in window) if window else 0
    if 0 < low_24h < price * (1 - tp1_pct / 100) * 0.999:
        tp2_pct = (price - low_24h) / price * 100
    else:
        tp2_pct = tp1_pct * 1.5

    return {
        "sl_pct": sl_pct, "tp_pct": tp1_pct, "tp2_pct": tp2_pct,
        "sl_basis": sl_basis, "swing_high_recent": swing_high_recent,
        "nearest_support": nearest_support,
        "sl_price": price * (1 + sl_pct / 100),
        "tp_price": price * (1 - tp1_pct / 100),
        "tp2_price": price * (1 - tp2_pct / 100),
    }


def compute_short_factors(klines, interval_minutes=240, symbol=None,
                          funding_rate_override=None, oi_series_override=None):
    closes = [k["close"] for k in klines]
    price = closes[-1]
    n = len(klines)

    rsi_val = analyzer.rsi(closes, 14)
    macd_data = analyzer.macd(closes)
    atr_val = analyzer.atr(klines, 14)
    atr_pct = (atr_val / price * 100) if price > 0 else 0

    swing = analyzer.find_swing_points(klines, 5)
    struct = analyzer.analyze_structure(klines, swing["swing_highs"], swing["swing_lows"])
    struct_trend = struct["trend"]
    vol = analyzer.analyze_volume(klines)
    patterns = analyzer.detect_candlestick_patterns(klines)

    ema20 = analyzer.ema(closes, 20)
    ema50 = analyzer.ema(closes, 50)
    ema200 = analyzer.ema(closes, 200) if n >= 200 else None
    ema_bear = (ema20 < ema50 < ema200) if ema200 is not None else (ema20 < ema50)
    price_below_ema20 = price < ema20

    bars_24h = max(1, int(1440 / interval_minutes))
    window = klines[-bars_24h:]
    hi = max(k["high"] for k in window)
    lo = min(k["low"] for k in window)
    rng = hi - lo
    range_pct = ((price - lo) / rng * 100) if rng > 0 else 50.0

    factors = []  # (name, ok, detail)

    # 1. RSI
    if rsi_val > 70:
        factors.append(("RSI超买", True, f"RSI={rsi_val:.0f}"))
    elif rsi_val > 60:
        factors.append(("RSI偏高", True, f"RSI={rsi_val:.0f}"))
    else:
        state = "中性" if rsi_val >= 45 else "偏低"
        factors.append(("RSI超买", False, f"RSI={rsi_val:.0f} {state}"))

    # 2. EMA空头排列
    if ema200 is not None:
        ema_desc = f"EMA20={ema20:.6g} 50={ema50:.6g} 200={ema200:.6g}"
        if ema_bear:
            factors.append(("EMA空头排列", True, ema_desc))
        else:
            order = "多头排列" if ema20 > ema50 else "均线纠缠"
            factors.append(("EMA空头排列", False, f"{ema_desc} → {order}"))
    else:
        ema_desc = f"EMA20={ema20:.6g} 50={ema50:.6g}"
        if ema_bear:
            factors.append(("EMA空头排列(20<50)", True, ema_desc))
        else:
            order = "多头排列" if ema20 > ema50 else "均线纠缠"
            factors.append(("EMA空头排列(20<50)", False, f"{ema_desc} → {order}"))

    # 3. 价格<EMA20
    diff_pct = (price - ema20) / ema20 * 100
    pos = "上方" if diff_pct > 0 else "下方"
    if price_below_ema20:
        factors.append(("价格<EMA20", True, f"价格{price:.6g} < EMA20 {ema20:.6g} ({pos} {abs(diff_pct):.1f}%)"))
    else:
        factors.append(("价格<EMA20", False, f"价格{price:.6g} 在 EMA20 {ema20:.6g} {pos} {abs(diff_pct):.1f}%"))

    # 4. 下跌结构
    struct_ok = struct_trend in ("bearish", "bullish_div")
    factors.append(("下跌结构", struct_ok, f"结构: {_trend_cn(struct_trend)}"))

    # 5. 看跌形态
    bearish_pattern = patterns.get("bearish_engulfing") or patterns.get("pin_bar_bearish_c3") or patterns.get("fake_break_above")
    if bearish_pattern:
        pat = "看跌吞没" if patterns.get("bearish_engulfing") else ("看跌pin bar" if patterns.get("pin_bar_bearish_c3") else "假突破上轨")
        factors.append(("看跌形态", True, f"当前K线: {pat}"))
    else:
        actual = "无显著形态"
        if patterns.get("bullish_engulfing"): actual = "看涨吞没"
        elif patterns.get("pin_bar_bullish_c3") or patterns.get("pin_bar_bullish_c2"): actual = "看涨pin bar"
        elif patterns.get("fake_break_below"): actual = "假突破下轨"
        elif patterns.get("doji_c3"): actual = "十字星(犹豫)"
        factors.append(("看跌形态", False, f"当前K线: {actual}"))

    # 6. 24h区间高位
    if range_pct > 70:
        factors.append(("24h区间高位", True, f"位置{range_pct:.0f}% (低{lo:.6g}/高{hi:.6g})"))
    else:
        pos_cn = "偏上" if range_pct > 55 else ("中位" if range_pct > 30 else "低位")
        factors.append(("24h区间高位", False, f"位置{range_pct:.0f}% 区间{pos_cn} (低{lo:.6g}/高{hi:.6g})"))

    # 7. MACD柱下降
    hist_cn = {"falling": "下降", "rising": "上升", "neutral": "走平"}.get(macd_data["hist_trend"], macd_data["hist_trend"])
    factors.append(("MACD柱下降", macd_data["hist_trend"] == "falling", f"柱状趋势: {hist_cn}"))

    # 8. MACD顶背离
    bear_div = False
    if len(closes) >= 60 and len(macd_data["macd_line_series"]) >= 5:
        recent_macd = macd_data["macd_line_series"]
        p0, p5 = closes[-1], closes[-5]
        m0, m5 = recent_macd[-1], recent_macd[0]
        if p0 > p5 * 1.005 and m0 < m5 * 0.995:
            bear_div = True
    if bear_div:
        factors.append(("MACD顶背离", True, "价创新高但MACD走低"))
    else:
        bull_div = False
        if len(closes) >= 60 and len(macd_data["macd_line_series"]) >= 5:
            p0, p5 = closes[-1], closes[-5]
            m0, m5 = macd_data["macd_line_series"][-1], macd_data["macd_line_series"][0]
            if p0 < p5 * 0.995 and m0 > m5 * 1.005:
                bull_div = True
        if bull_div:
            factors.append(("MACD顶背离", False, "存在底背离(价新低MACD走高)"))
        else:
            factors.append(("MACD顶背离", False, "无背离"))

    # 9. 放量看跌
    vol_bear = vol["signal"] == "volume_spike" and bool(patterns.get("bearish_engulfing") or patterns.get("pin_bar_bearish_c3"))
    if vol_bear:
        factors.append(("放量看跌", True, f"量比{vol['ratio']:.1f}x 放量+看跌K线"))
    else:
        factors.append(("放量看跌", False, f"量比{vol['ratio']:.1f}x {_vol_cn(vol['signal'])}"))

    # 10. 缩量上涨
    vol_weak_up = vol["signal"] == "volume_shrink" and vol["ratio"] < 0.4 and struct_trend == "bullish"
    if vol_weak_up:
        factors.append(("缩量上涨", True, f"量比{vol['ratio']:.1f}x 缩量+上涨结构"))
    else:
        factors.append(("缩量上涨", False, f"量比{vol['ratio']:.1f}x {_vol_cn(vol['signal'])}，结构{_trend_cn(struct_trend)}"))

    # 11. 资金费率（聪明钱·多头拥挤度）
    if symbol:
        try:
            if funding_rate_override is not None:
                fr = float(funding_rate_override)
            else:
                fr = analyzer.fetch_funding_rate(symbol)["funding_rate"]
            if fr >= 0.0005:
                factors.append(("资金费率多头拥挤", True, f"费率{fr*100:+.3f}% 多头拥挤"))
            elif fr <= -0.0005:
                factors.append(("资金费率多头拥挤", False, f"费率{fr*100:+.3f}% 空头拥挤(做空危险)"))
            else:
                factors.append(("资金费率多头拥挤", False, f"费率{fr*100:+.3f}% 中性"))
        except Exception:
            factors.append(("资金费率多头拥挤", None, "费率获取失败"))
    else:
        factors.append(("资金费率多头拥挤", None, "无symbol跳过"))

    # 12. 持仓量配合（聪明钱·OI背离）
    if symbol:
        oi_series = oi_series_override if oi_series_override is not None else fetch_oi_trend(symbol)
        if oi_series and len(oi_series) >= 5:
            oi_now = oi_series[-1]
            oi_prev = oi_series[-5]  # 4个4h周期前 ≈ 16h前
            oi_chg = (oi_now / oi_prev - 1) * 100 if oi_prev > 0 else 0
            px_prev = closes[-5]
            px_chg = (price / px_prev - 1) * 100 if px_prev > 0 else 0
            if px_chg > 0.5 and oi_chg < -2:
                factors.append(("持仓量配合看空", True, f"OI {oi_chg:+.1f}% 价涨仓减(多头离场)"))
            elif px_chg < -0.5 and oi_chg > 2:
                factors.append(("持仓量配合看空", True, f"OI {oi_chg:+.1f}% 价跌仓增(空头进场)"))
            elif px_chg > 0.5 and oi_chg > 2:
                factors.append(("持仓量配合看空", False, f"OI {oi_chg:+.1f}% 价涨仓增(资金做多)"))
            elif px_chg < -0.5 and oi_chg < -2:
                factors.append(("持仓量配合看空", False, f"OI {oi_chg:+.1f}% 价跌仓减(空头获利离场)"))
            else:
                factors.append(("持仓量配合看空", False, f"OI {oi_chg:+.1f}% / 价格{px_chg:+.1f}% 变化不明显"))
        else:
            factors.append(("持仓量配合看空", None, "OI数据获取失败"))
    else:
        factors.append(("持仓量配合看空", None, "无symbol跳过"))

    valid = [f for f in factors if f[1] is not None]
    triggered = [name for name, ok, _ in valid if ok]
    n_hit = len(triggered)
    confidence = 50 + (n_hit / len(factors)) * 50
    return {
        "n_hit": n_hit, "triggered": triggered, "factors": factors,
        "atr_pct": atr_pct, "confidence": confidence,
        "rsi": rsi_val, "range_pct": range_pct, "price": price,
        "n_total": len(valid),
    }


def build_report(symbol: str) -> str:
    try:
        klines = analyzer.fetch_klines(symbol, INTERVAL, 300)
    except Exception as e:
        return f"❌ 获取 {symbol} K线失败: {e}"
    if len(klines) < 60:
        return f"❌ {symbol} 数据严重不足（仅{len(klines)}根4h K线，需≥60）"

    sig = compute_short_factors(klines, symbol=symbol)
    price = sig["price"]
    atr_pct = sig["atr_pct"]
    n_hit = sig["n_hit"]
    n_total = sig["n_total"]
    conf = sig["confidence"]
    lev = leverage_for_atr(atr_pct, conf)

    # 回测验证的 ATR SL/TP（2026-08-25 backtest_4h.py + backtest_4h_scan.py）
    # ❌ 旧参数 1.5×ATR/3×ATR：近20天四币全负（SL命中60-86% vs TP命中0-14%）
    #    —— 止损太紧被扫、止盈太远够不着，正期望被吃光
    # ✅ 新参数 1.0×ATR/1.0×ATR（盈亏比1:1）：SPCX 57%胜率 PF1.11、BTC 67% PF1.52
    sl_pct = max(atr_pct * 1.0, 0.5)
    tp_pct = atr_pct * 1.0
    sl_price = price * (1 + sl_pct / 100)
    tp_price = price * (1 - tp_pct / 100)

    # V2.0 移植：结构止损 + 动态TP + Market Regime（对照展示，回测验证后定默认）
    swing = analyzer.find_swing_points(klines, 5)
    struct = structural_sl_tp(price, atr_pct, klines, swing["swing_highs"], swing["swing_lows"])
    regime = detect_regime(symbol)

    now = datetime.datetime.now().strftime("%m-%d %H:%M")
    is_alt = symbol[:-4] in ALTS if symbol.endswith("USDT") else symbol in ALTS

    reg_tag = f"  |  Regime: {regime['regime']}" if regime else ""
    lines = []
    lines.append(f"📊 {symbol} 4h做空信号 ({now})")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"价格: {price:.6g}  |  ATR: {atr_pct:.2f}%  |  RSI: {sig['rsi']:.0f}{reg_tag}")
    lines.append(f"24h区间位置: {sig['range_pct']:.0f}%  |  数据: {len(klines)}根")
    if len(klines) < 200:
        lines.append("⚠️ 上线不足200根，EMA200因子降级（EMA20<50替代）")
    lines.append("")

    status = "✅ 可入场做空" if n_hit >= MIN_FACTORS else "⛔ 观望"
    lines.append(f"{status} ({n_hit}/{n_total} 因子，需≥{MIN_FACTORS})")
    lines.append("─" * 24)
    for name, ok, detail in sig["factors"]:
        if ok is None:
            lines.append(f"  ⚠ {name} ({detail})")
        else:
            mark = "✓" if ok else "✗"
            lines.append(f"  {mark} {name} ({detail})")
    lines.append("")
    if n_hit >= MIN_FACTORS:
        lines.append(f"建议杠杆: {lev}x (置信度{conf:.0f})")
        lines.append(f"止损(ATR): {sl_price:.6g} (+{sl_pct:.1f}%)")
        lines.append(f"止盈(ATR): {tp_price:.6g} (-{tp_pct:.1f}%)")
        # V2.0 结构止损对照
        s_basis = struct["sl_basis"]
        lines.append(f"止损(结构): {struct['sl_price']:.6g} (+{struct['sl_pct']:.1f}%, 依据:{s_basis})")
        lines.append(f"止盈(结构): {struct['tp_price']:.6g} (-{struct['tp_pct']:.1f}%)  → TP2 {struct['tp2_price']:.6g}")
        lines.append(f"最大持仓: 16根4h (≈2.7天)")
    else:
        lines.append("📌 无信号不交易。今天不做，明天还有机会。")

    lines.append("")
    if is_alt:
        lines.append("⚠️ 山寨币: 禁止做多（实盘11%胜率，回测0%）")
    else:
        lines.append("ℹ️ 主流币: 做多需单独确认趋势")
    lines.append("⚠️ 规则: 只做空·不追价·止损必挂·连亏2笔停24h")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("❌ 用法: python3 coin_analysis_4h.py <币种>")
        sys.exit(1)

    raw = sys.argv[1].strip().upper()
    symbol = raw if raw.endswith("USDT") else raw + "USDT"

    try:
        result = build_report(symbol)
    except Exception as e:
        print(f"❌ 分析出错: {e}")
        sys.exit(1)

    if len(result) <= MAX_CHARS:
        print(result)
        return

    lines = result.split('\n')
    kept = []
    for line in lines:
        kept.append(line)
        if len('\n'.join(kept)) > MAX_CHARS:
            kept.pop()
            break
    print('\n'.join(kept))


if __name__ == "__main__":
    main()
