#!/usr/bin/env python3
"""
backtest_v2.py — V2.0 系统历史回测（无未来函数）
==================================================
用法: python3 backtest_v2.py [SYMBOL] [DAYS]
默认: BTCUSDT 20 天（衍生品历史覆盖上限 ~20.8 天，币安 limit=500）

方法：
  决策点 = 每根 1h K线收盘时刻（最近 N 天，全部采样）
  每个决策点只用截至该时刻【已收盘】数据构造完整上下文：
    - K线切片: 15m×200 / 5m×100 / 1m×60（决策点前已收盘）
    - ticker:  从前 24 根 1h 构造（change_pct/high/low/quote_volume）
    - funding: 历史 fundingRate 序列取最近值 → signal 判定（与线上同阈值）
    - derivatives: 历史 OI(1h粒度)/Taker(1h粒度) 取决策点时刻值
                  （5m/15m OI 变化无历史 → 降级用 1h 变化）
    - regime/btc_regime: 截至 t 的 4h/1h 计算（detect_market_regime）
    - whale: 中性（实时 orderbook 无历史，无法回测）
  评估：信号后 60m/4h 窗口，5m 粒度追踪 SL/TP/收益（价格收益，不含杠杆）

输出：方向胜率 / regime分桶 / 置信度分桶 / EntryTrigger分桶 / 硬过滤对照
"""

import json
import sys
import time
from datetime import datetime, timezone

from analyzer import fetch_klines, score_system, risk_recommendation
from futures_data import fetch_oi_series, fetch_funding_series, fetch_taker_series
from futures_data import detect_market_regime

TZ = timezone.utc
WHALE_NEUTRAL = {"whale_score": 50.0, "confidence": 0.0,
                 "grade_label": "中性 ➖", "factors": {}}


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%m-%d %H:%M")


def fetch_history(symbol: str, interval: str, days: int, limit: int = 1000) -> list[dict]:
    """分页拉历史 K 线（升序）。closed_only 恒 True（历史全已收盘）。"""
    now_ms = int(time.time() * 1000)
    start = now_ms - days * 86400_000
    out, seen = [], set()
    while start < now_ms:
        batch = fetch_klines(symbol, interval, limit, closed_only=True, start_time=start)
        if not batch:
            break
        for k in batch:
            if k["time"] not in seen:
                seen.add(k["time"])
                out.append(k)
        if len(batch) < limit:
            break
        start = batch[-1]["time"] + 1
        time.sleep(0.15)  # 礼貌限速
    return out


def build_hist_ticker(t: int, klines_1h: list[dict]) -> dict:
    """构造决策点 t 的 24h ticker（t 前 24 根已收盘 1h）。"""
    wins = [k for k in klines_1h if k["close_time"] <= t][-24:]
    if len(wins) < 2:
        return {"lastPrice": 0, "priceChangePercent": 0, "highPrice": 0,
                "lowPrice": 0, "quoteVolume": 0, "count": 0}
    open_p = wins[0]["open"]
    close_p = wins[-1]["close"]
    return {
        "lastPrice": close_p,
        "priceChangePercent": (close_p - open_p) / open_p * 100 if open_p else 0,
        "highPrice": max(k["high"] for k in wins),
        "lowPrice": min(k["low"] for k in wins),
        "quoteVolume": sum(k["volume"] * k["close"] for k in wins),
        "count": 0,
    }


def build_funding_dict(t: int, funding_hist: list[dict]) -> dict:
    """决策点 t 的 funding dict（signal 判定与线上 fetch_funding_rate 同阈值）。"""
    pts = [p for p in funding_hist if p["time"] <= t]
    if not pts:
        return {"funding_rate": 0, "signal": "neutral"}
    fr = pts[-1]["rate"]
    signal = "neutral"
    if fr > 0.0005:
        signal = "long_crowded"
    elif fr < -0.0005:
        signal = "short_crowded"
    return {"funding_rate": fr, "signal": signal}


def build_hist_derivatives(t: int, oi_hist: list[dict], funding_hist: list[dict],
                           taker_hist: list[dict]) -> dict:
    """决策点 t 的衍生品上下文（纯历史构造，判定阈值与线上 build_derivatives_context 一致）。"""
    ctx = {
        "oi": None, "oi_change_5m": None, "oi_change_15m": None, "oi_change_1h": None,
        "oi_trend": "unknown",
        "funding_rate": None, "funding_series": [], "funding_trend": "flat",
        "funding_regime": "normal",
        "taker_ratio": None, "taker_buy_vol": None, "taker_sell_vol": None,
        "taker_ratio_1h_ago": None, "taker_trend": "neutral",
        "global_ls_ratio": None, "liq_5m_long": None, "liq_5m_short": None,
        "errors": {},
    }
    # OI（1h 粒度，回测限制：币安 OI 历史仅 5m/15m/1h/4h/1d 且 limit≤500）
    oi_pts = [p for p in oi_hist if p["time"] <= t]
    if len(oi_pts) >= 2:
        cur, prev = oi_pts[-1]["oi_value"], oi_pts[-2]["oi_value"]
        ctx["oi"] = oi_pts[-1]["oi"]
        if prev > 0:
            chg1h = (cur - prev) / prev * 100
            ctx["oi_change_1h"] = round(chg1h, 2)
            if chg1h > 1.0:
                ctx["oi_trend"] = "rising"
            elif chg1h < -1.0:
                ctx["oi_trend"] = "falling"
            else:
                ctx["oi_trend"] = "flat"
    # Funding
    fr_pts = [p for p in funding_hist if p["time"] <= t]
    if fr_pts:
        rates = [p["rate"] for p in fr_pts[-6:]]
        ctx["funding_rate"] = rates[-1]
        ctx["funding_series"] = fr_pts[-6:]
        if len(rates) >= 4:
            recent = rates[-4:]
            up = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
            dn = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i - 1])
            if up >= 3:
                ctx["funding_trend"] = "rising"
            elif dn >= 3:
                ctx["funding_trend"] = "falling"
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
    # Taker（1h 粒度）
    tk_pts = [p for p in taker_hist if p["time"] <= t]
    if len(tk_pts) >= 2:
        ctx["taker_ratio"] = tk_pts[-1]["ratio"]
        r_now, r_ago = tk_pts[-1]["ratio"], tk_pts[-2]["ratio"]
        ctx["taker_ratio_1h_ago"] = r_ago
        if r_now is not None and r_ago is not None:
            if r_now >= 1.1 and r_now > r_ago * 1.05:
                ctx["taker_trend"] = "buy_dominant"
            elif r_now <= 0.9 and r_now < r_ago * 0.95:
                ctx["taker_trend"] = "sell_dominant"
            else:
                ctx["taker_trend"] = "neutral"
    return ctx


def slice_before(klines: list[dict], t: int, n: int) -> list[dict]:
    return [k for k in klines if k["close_time"] <= t][-n:]


def evaluate_trade(entry: float, sl: float, tp: float, direction: str,
                   k5m_after: list[dict], t: int) -> dict:
    """5m 粒度追踪：60m/4h 双窗口。返回收益(%)与 SL/TP 命中。"""
    out = {}
    for label, horizon_ms in (("60m", 60 * 60_000), ("4h", 4 * 3600_000)):
        end_t = t + horizon_ms
        mfe = mae = 0.0
        ret = 0.0
        sl_hit = tp_hit = False
        last_close = entry
        for k in k5m_after:
            if k["close_time"] > end_t:
                break
            hi, lo = k["high"], k["low"]
            if direction == "LONG":
                mfe = max(mfe, (hi - entry) / entry * 100)
                mae = min(mae, (lo - entry) / entry * 100)
                if not tp_hit and hi >= tp:
                    tp_hit = True
                    ret = (tp - entry) / entry * 100
                    break
                if lo <= sl:
                    sl_hit = True
                    ret = (sl - entry) / entry * 100
                    break
            else:
                mfe = max(mfe, (entry - lo) / entry * 100)
                mae = min(mae, (entry - hi) / entry * 100)
                if not tp_hit and lo <= tp:
                    tp_hit = True
                    ret = (entry - tp) / entry * 100
                    break
                if hi >= sl:
                    sl_hit = True
                    ret = (entry - sl) / entry * 100
                    break
            last_close = k["close"]
        if not (sl_hit or tp_hit):
            ret = ((last_close - entry) / entry * 100) if direction == "LONG" \
                else ((entry - last_close) / entry * 100)
        out[label] = {"ret": ret, "mfe": mfe, "mae": mae, "sl_hit": sl_hit, "tp_hit": tp_hit}
    return out


def agg(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for r in rows if r["ret_4h"] > 0)
    return {
        "n": n,
        "win_rate": round(wins / n * 100, 1),
        "avg_ret": round(sum(r["ret_4h"] for r in rows) / n, 3),
        "avg_mfe": round(sum(r["mfe_4h"] for r in rows) / n, 3),
        "avg_mae": round(sum(r["mae_4h"] for r in rows) / n, 3),
        "sl_hit": round(sum(1 for r in rows if r["sl_hit_4h"]) / n * 100, 1),
        "tp_hit": round(sum(1 for r in rows if r["tp_hit_4h"]) / n * 100, 1),
        "win_rate_60m": round(sum(1 for r in rows if r["ret_60m"] > 0) / n * 100, 1),
    }


def main():
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "BTCUSDT"
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    print(f"⏳ 拉取历史数据 {symbol} ({days}天)...", flush=True)
    t0 = time.time()
    k5m = fetch_history(symbol, "5m", days + 1)
    k15m = fetch_history(symbol, "15m", days + 3)
    k1m = fetch_history(symbol, "1m", days)
    k1h = fetch_history(symbol, "1h", days + 12)
    k4h = fetch_history(symbol, "4h", days + 40)
    oi_hist = fetch_oi_series(symbol, "1h", 500) or []
    funding_hist = fetch_funding_series(symbol, 500) or []
    taker_hist = fetch_taker_series(symbol, "1h", 500) or []
    print(f"✅ 数据就绪: 5m×{len(k5m)} 15m×{len(k15m)} 1m×{len(k1m)} "
          f"1h×{len(k1h)} 4h×{len(k4h)} OI×{len(oi_hist)} Funding×{len(funding_hist)} "
          f"Taker×{len(taker_hist)} ({time.time()-t0:.0f}s)", flush=True)

    # 决策区间：衍生品历史覆盖范围内
    deriv_end = min((oi_hist[-1]["time"] if oi_hist else 10**18),
                    (taker_hist[-1]["time"] if taker_hist else 10**18))
    decision_ts = sorted(k["close_time"] for k in k1h
                         if k["close_time"] <= deriv_end and k["close_time"] >= k1h[0]["close_time"] + 24 * 3600_000)
    print(f"决策点: {len(decision_ts)} 个 (区间 {fmt_ts(decision_ts[0])} ~ {fmt_ts(decision_ts[-1])})", flush=True)

    k5m_by_time = k5m
    trades = []       # 有明确方向(≥60%)的信号
    blocked = []      # 被硬过滤拦下的信号（对照）
    all_rows = []     # 全部决策点（含 NEUTRAL，不入 trade 统计）

    for i, t in enumerate(decision_ts):
        if i % 60 == 0:
            print(f"  ... {i}/{len(decision_ts)} ({fmt_ts(t)})", flush=True)
        price = next((k["close"] for k in reversed(k1h) if k["close_time"] == t), None)
        if price is None:
            continue
        ticker = build_hist_ticker(t, k1h)
        if ticker["lastPrice"] == 0:
            continue
        funding = build_funding_dict(t, funding_hist)
        derivatives = build_hist_derivatives(t, oi_hist, funding_hist, taker_hist)
        c15 = slice_before(k15m, t, 200)
        c5 = slice_before(k5m, t, 100)
        c1 = slice_before(k1m, t, 60)
        c1h = slice_before(k1h, t, 200)
        c4h = slice_before(k4h, t, 200)
        if len(c15) < 60 or len(c5) < 30:
            continue
        try:
            regime = detect_market_regime(c4h, c1h)
            score = score_system(price, c15, c5, c1, ticker, funding, symbol,
                                 derivatives=derivatives, regime=regime,
                                 btc_regime=regime, whale_override=WHALE_NEUTRAL)
        except Exception:
            continue
        lp, sp = score.get("long_probability", 50), score.get("short_probability", 50)
        if max(lp, sp) < 60:
            continue  # 无明确方向，不构成信号
        direction = "LONG" if lp > sp else "SHORT"
        try:
            risk = risk_recommendation(price, score, 1000)
        except Exception:
            continue
        entry, sl, tp = risk["entry_price"], risk["stop_loss"], risk["take_profit"]
        if not (entry and sl and tp):
            continue
        # 评估：决策点后 5m 已收盘 K 线
        k5m_after = [k for k in k5m_by_time if k["close_time"] > t]
        ev = evaluate_trade(entry, sl, tp, direction, k5m_after, t)

        row = {
            "time": t, "ts": fmt_ts(t), "price": price, "direction": direction,
            "regime": score.get("market_regime"), "grade": score.get("grade"),
            "confidence": risk["confidence"],
            "entry_trigger": score.get("entry_trigger"),
            "no_trade": bool(score.get("no_trade")),
            "sl_pct": risk["sl_pct"], "tp_pct": risk["tp_pct"],
            "rr": risk["rr_ratio"], "sl_basis": risk.get("sl_basis"),
            "ret_4h": ev["4h"]["ret"], "mfe_4h": ev["4h"]["mfe"], "mae_4h": ev["4h"]["mae"],
            "sl_hit_4h": ev["4h"]["sl_hit"], "tp_hit_4h": ev["4h"]["tp_hit"],
            "ret_60m": ev["60m"]["ret"],
        }
        all_rows.append(row)
        if row["no_trade"]:
            blocked.append(row)
        else:
            trades.append(row)

    # ── 报告 ──
    print("\n" + "=" * 62)
    print(f"  V2.0 回测报告 | {symbol} | {days}天 | {fmt_ts(decision_ts[0])} ~ {fmt_ts(decision_ts[-1])}")
    print("=" * 62)
    print(f"  决策点: {len(decision_ts)} | 方向信号: {len(all_rows)} | 可交易(未被硬过滤): {len(trades)} | 被拦下: {len(blocked)}")
    print(f"  ⚠️  whale 因子中性化（orderbook 无历史）; OI/Taker 用 1h 粒度近似")

    def show(title, rows):
        a = agg(rows)
        if a["n"] == 0:
            print(f"\n  {title}: 无样本")
            return
        print(f"\n  {title}: n={a['n']} | 胜率(4h) {a['win_rate']}% | 60m胜率 {a['win_rate_60m']}% | "
              f"avg_ret {a['avg_ret']:+.3f}% | MFE {a['avg_mfe']:.2f}% MAE {a['avg_mae']:.2f}% | "
              f"SL命中 {a['sl_hit']}% TP命中 {a['tp_hit']}%")

    show("全部方向信号", all_rows)
    show("   ├ 可交易(硬过滤放行)", trades)
    show("   └ 被硬过滤拦下(对照)", blocked)

    for d in ("LONG", "SHORT"):
        show(f"按方向 [{d}]", [r for r in trades if r["direction"] == d])

    # regime 分桶（可交易集）
    print("\n  ── regime × 方向 ──")
    for reg in sorted({r["regime"] for r in trades}):
        for d in ("LONG", "SHORT"):
            rows = [r for r in trades if r["regime"] == reg and r["direction"] == d]
            if rows:
                show(f"  {reg} + {d}", rows)
    # 顺势/逆势
    def trend_kind(r):
        reg, d = r["regime"], r["direction"]
        if ("UP" in reg and d == "LONG") or ("DOWN" in reg and d == "SHORT"):
            return "顺势"
        if ("UP" in reg and d == "SHORT") or ("DOWN" in reg and d == "LONG"):
            return "逆势"
        return "震荡/其他"
    for kind in ("顺势", "逆势", "震荡/其他"):
        show(f"趋势属性 [{kind}]", [r for r in trades if trend_kind(r) == kind])

    # 置信度分桶
    print("\n  ── 置信度分桶 ──")
    for lo, hi in ((60, 70), (70, 80), (80, 101)):
        show(f"  置信度 {lo}-{hi}%", [r for r in trades if lo <= r["confidence"] < hi])

    # Entry Trigger 分桶
    print("\n  ── Entry Trigger ──")
    for st in ("READY", "WAIT"):
        show(f"  {st}", [r for r in trades if r["entry_trigger"] == st])

    # 止损依据分桶
    print("\n  ── 止损依据 ──")
    for b in ("结构低点", "结构高点", "ATR"):
        show(f"  {b}", [r for r in trades if r["sl_basis"] == b])

    # 落盘
    out_path = f"backtest_{symbol}_{days}d.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"symbol": symbol, "days": days,
                   "decision_ts": [fmt_ts(x) for x in (decision_ts[0], decision_ts[-1])],
                   "rows": all_rows}, f, ensure_ascii=False, indent=1)
    print(f"\n📁 明细已存: {out_path} (共 {len(all_rows)} 条)")


if __name__ == "__main__":
    main()
