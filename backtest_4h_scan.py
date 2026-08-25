#!/usr/bin/env python3
"""
backtest_4h_scan.py — 4h做空系统 SL/TP 参数扫描（无未来函数）
==============================================================
背景：backtest_4h.py 发现最近 20 天机械 SL/TP（1.5×ATR / 3×ATR）全负期望，
SL 命中 60-86% / TP 命中 0-14% → 止损太紧、止盈太远。
本脚本对同一批信号扫描 SL/TP 乘数组合，判断是信号失效还是参数失配。

用法: python3 backtest_4h_scan.py [SYMBOL] [DAYS]
默认: SPCXUSDT 20 天
"""
import sys
import time
from datetime import datetime, timezone

from analyzer import fetch_klines, find_swing_points
from futures_data import fetch_oi_series, fetch_funding_series
from coin_analysis_4h import compute_short_factors, MIN_FACTORS
from backtest_4h import fetch_history, slice_before, funding_at, oi_series_at, MAX_HOLD_MS

TZ = timezone.utc

# SL/TP 扫描网格（ATR 乘数）
SL_MULTS = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]
TP_MULTS = [0.5, 1.0, 1.5, 2.0, 3.0]
# 保守约定：同一根 5m bar 内 SL/TP 都触及 → 先判 SL（与 7/31 backtest_strategy 一致）


def evaluate_short_conservative(entry, sl, tp, k5m_after, t):
    """先判 SL 后判 TP（保守）。返回 (ret%, sl_hit, tp_hit)。"""
    end_t = t + MAX_HOLD_MS
    ret = 0.0
    sl_hit = tp_hit = False
    last_close = entry
    for k in k5m_after:
        if k["close_time"] > end_t:
            break
        hi, lo = k["high"], k["low"]
        if hi >= sl:                      # SL 优先（保守）
            sl_hit = True
            ret = (entry - sl) / entry * 100
            break
        if lo <= tp:
            tp_hit = True
            ret = (entry - tp) / entry * 100
            break
        last_close = k["close"]
    if not (sl_hit or tp_hit):
        ret = (entry - last_close) / entry * 100
    return ret, sl_hit, tp_hit


def main():
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "SPCXUSDT"
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    print(f"⏳ 拉取历史数据 {symbol} ({days}天)...", flush=True)
    t0 = time.time()
    k4h = fetch_history(symbol, "4h", days + 15)
    k5m = fetch_history(symbol, "5m", days + 2)
    oi_hist = fetch_oi_series(symbol, "1h", 500) or []
    funding_hist = fetch_funding_series(symbol, 500) or []
    print(f"✅ 数据就绪: 4h×{len(k4h)} 5m×{len(k5m)} OI×{len(oi_hist)} Funding×{len(funding_hist)} "
          f"({time.time()-t0:.0f}s)", flush=True)

    deriv_end = oi_hist[-1]["time"]
    deriv_start = max(oi_hist[0]["time"], k4h[0]["close_time"] + 60 * 4 * 3600_000)
    decision_ts = sorted(k["close_time"] for k in k4h
                         if deriv_start <= k["close_time"] <= deriv_end)

    # 收集信号（每笔只算一次因子 + 5m 轨迹）
    signals = []  # (entry, atr_pct, k5m_after, t)
    for t in decision_ts:
        c4h = slice_before(k4h, t, 300)
        if len(c4h) < 60:
            continue
        try:
            sig = compute_short_factors(c4h, interval_minutes=240, symbol=symbol,
                                        funding_rate_override=funding_at(t, funding_hist),
                                        oi_series_override=oi_series_at(t, oi_hist, 9))
        except Exception:
            continue
        if sig["n_hit"] < MIN_FACTORS:
            continue
        k5m_after = [k for k in k5m if k["close_time"] > t]
        signals.append((sig["price"], sig["atr_pct"], k5m_after, t))

    print(f"信号数: {len(signals)} (决策点 {len(decision_ts)})\n", flush=True)
    if not signals:
        print("❌ 无信号")
        sys.exit(1)

    # 基准：无止损（SL=20×ATR ≈ 不触发），固定 TP + 时间退出
    def run(sl_mult, tp_mult):
        rets, sls, tps = [], 0, 0
        for entry, atr_pct, k5m_after, t in signals:
            sl = entry * (1 + max(atr_pct * sl_mult, 0.5) / 100)
            tp = entry * (1 - atr_pct * tp_mult / 100)
            r, sh, th = evaluate_short_conservative(entry, sl, tp, k5m_after, t)
            rets.append(r); sls += sh; tps += th
        n = len(rets)
        wins = sum(1 for r in rets if r > 0)
        gross_w = sum(r for r in rets if r > 0)
        gross_l = abs(sum(r for r in rets if r <= 0))
        pf = gross_w / gross_l if gross_l > 0 else float("inf")
        return {"n": n, "win": wins / n * 100, "avg": sum(rets) / n,
                "pf": pf, "sl": sls / n * 100, "tp": tps / n * 100,
                "total": sum(rets)}

    print(f"{'SL\\TP':<8}" + "".join(f"{f'TP={m}':>14}" for m in TP_MULTS))
    print("-" * 78)
    results = {}
    for sm in SL_MULTS:
        line = f"SL={sm:<4}"
        for tm in TP_MULTS:
            r = run(sm, tm)
            results[(sm, tm)] = r
            cell = f"{r['win']:.0f}%/{r['avg']:+.2f}%"
            line += f"{cell:>14}"
        print(line)
    print("-" * 78)
    print("格式: 胜率/avg_ret（价格收益%，不含杠杆）")

    # 基准：无止损
    r0 = run(20.0, 3.0)
    print(f"\n基准(无止损,TP=3×ATR): 胜率{r0['win']:.0f}% avg {r0['avg']:+.2f}% PF {r0['pf']:.2f} "
          f"SL{r0['sl']:.0f}% TP{r0['tp']:.0f}% 总{r0['total']:+.1f}%")

    # 最优组合
    best = max(results.items(), key=lambda kv: kv[1]["avg"])
    print(f"最优 avg_ret: SL={best[0][0]}×ATR TP={best[0][1]}×ATR → "
          f"胜率{best[1]['win']:.0f}% avg {best[1]['avg']:+.2f}% PF {best[1]['pf']:.2f} "
          f"SL{best[1]['sl']:.0f}% TP{best[1]['tp']:.0f}% 总{best[1]['total']:+.1f}%")
    best_pf = max(results.items(), key=lambda kv: kv[1]["pf"] if kv[1]["pf"] != float("inf") else 0)
    print(f"最优 PF: SL={best_pf[0][0]}×ATR TP={best_pf[0][1]}×ATR → PF {best_pf[1]['pf']:.2f} "
          f"胜率{best_pf[1]['win']:.0f}% avg {best_pf[1]['avg']:+.2f}%")


if __name__ == "__main__":
    main()
