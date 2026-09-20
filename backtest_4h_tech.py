#!/usr/bin/env python3
"""
backtest_4h_tech.py — 4h做空系统【长周期纯技术面】回测（无未来函数）
=====================================================================
背景：backtest_4h.py 的决策区间被 OI 历史硬卡在最近 ~20.8 天
      （fetch_oi_series 1h×limit=500），导致 4h 系统「正期望」的全部
      实证只建立在 20 天窗口上——与 V2.1 顺势LONG 踩的坑同构
      （20天 PF 2.21 → 60天 PF 0.76）。

本脚本用 derivatives_disabled=True 跳过资金费率/OI 两个衍生品因子
（10 个纯技术因子为分母），从而把回测拉到 60~180 天，
覆盖【上涨 / 下跌 / 洗盘】多种行情，回答：
    4h 做空信号的正期望是真 edge，还是行情 beta ？

用法: python3 backtest_4h_tech.py [SYMBOL] [DAYS] [SL_MULT] [TP_MULT]
默认: SPCXUSDT 90 天，SL=1.0×ATR TP=1.0×ATR（实盘现行参数）

约定：
  - 决策点 = 每根 4h K线收盘时刻；只用截至该时刻已收盘的数据
  - 需要 ≥200 根 4h 历史（EMA200 生效，与实盘一致）
  - 5m 粒度追踪，最大持仓 16 根 4h（64h）；同一根内 SL/TP 都触及 → 先判 TP
    （与 backtest_4h.py 一致，保守性由 SL 在上方、距离更近部分抵消）
  - 组合模拟：$1000 起始，每笔固定风险 2%
"""
import json
import sys
import time
from datetime import datetime, timezone

from analyzer import find_swing_points
from futures_data import detect_market_regime
from coin_analysis_4h import compute_short_factors, structural_sl_tp, MIN_FACTORS
from backtest_4h import (fetch_history, slice_before, evaluate_short, simulate,
                         MAX_HOLD_MS, MIN_TECH_FACTORS)

TZ = timezone.utc
WARMUP_BARS = 200


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%m-%d %H:%M")


def month_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m")


def eval_short(entry, sl, tp, k5m_after, t, sl_first=False):
    """5m 粒度追踪 SHORT。sl_first=True 时同一根内先判 SL（保守）。"""
    end_t = t + MAX_HOLD_MS
    ret = 0.0
    sl_hit = tp_hit = False
    last_close = entry
    for k in k5m_after:
        if k["close_time"] > end_t:
            break
        hi, lo = k["high"], k["low"]
        sl_now, tp_now = hi >= sl, lo <= tp
        if sl_first and sl_now:
            sl_hit, ret = True, (entry - sl) / entry * 100
            break
        if tp_now:
            tp_hit, ret = True, (entry - tp) / entry * 100
            break
        if sl_now:
            sl_hit, ret = True, (entry - sl) / entry * 100
            break
        last_close = k["close"]
    if not (sl_hit or tp_hit):
        ret = (entry - last_close) / entry * 100
    return {"ret": ret, "sl_hit": sl_hit, "tp_hit": tp_hit}


def main():
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "SPCXUSDT"
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    sl_mult = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    tp_mult = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0

    print(f"⏳ [纯技术面降级] 拉取 {symbol} {days}天 数据（含 {WARMUP_BARS}根4h 预热）...", flush=True)
    t0 = time.time()
    k4h = fetch_history(symbol, "4h", days + 60)
    k1h = fetch_history(symbol, "1h", days + 60)
    k5m = fetch_history(symbol, "5m", days + 3)
    print(f"✅ 数据就绪: 4h×{len(k4h)} 1h×{len(k1h)} 5m×{len(k5m)} ({time.time()-t0:.0f}s)", flush=True)
    if len(k4h) <= WARMUP_BARS + 5 or not k5m:
        print("❌ 数据不足")
        sys.exit(1)

    now_ms = int(time.time() * 1000)
    cut = now_ms - MAX_HOLD_MS  # 保证有足够 5m 数据评估
    decision_ts = [k["close_time"] for i, k in enumerate(k4h)
                   if i >= WARMUP_BARS and k["close_time"] <= cut]
    if not decision_ts:
        print("❌ 无决策点")
        sys.exit(1)
    print(f"决策点: {len(decision_ts)} 个 (区间 {fmt_ts(decision_ts[0])} ~ {fmt_ts(decision_ts[-1])})", flush=True)

    trades = []
    for i, t in enumerate(decision_ts):
        if i % 50 == 0:
            print(f"  ... {i}/{len(decision_ts)} ({fmt_ts(t)})", flush=True)
        c4h = slice_before(k4h, t, 400)
        c1h = slice_before(k1h, t, 200)
        if len(c4h) < WARMUP_BARS:
            continue
        try:
            sig = compute_short_factors(c4h, interval_minutes=240, symbol=symbol,
                                        derivatives_disabled=True)
        except Exception:
            continue
        n_hit = sig["n_hit"]
        n_total = sig["n_total"]
        if n_hit < MIN_TECH_FACTORS:
            continue

        price = sig["price"]
        atr_pct = sig["atr_pct"]
        swing = find_swing_points(c4h, 5)
        try:
            regime = detect_market_regime(c4h, c1h)
            regime_name = regime.get("regime", "CHAOS")
            high_vol = regime.get("high_volatility", False)
        except Exception:
            regime_name, high_vol = "CHAOS", False

        # 30 天前价格（判断信号前的中期趋势，用于 beta 归因）
        px_30d = c4h[-180]["close"] if len(c4h) >= 180 else c4h[0]["close"]
        ret_30d = (price / px_30d - 1) * 100 if px_30d else 0.0

        sl_a = max(atr_pct * sl_mult, 1.0) if sl_mult >= 1.0 else atr_pct * sl_mult
        tp_a = atr_pct * tp_mult
        st = structural_sl_tp(price, atr_pct, c4h, swing["swing_highs"], swing["swing_lows"])

        k5m_after = [k for k in k5m if k["close_time"] > t]
        sl_price_a, tp_price_a = price * (1 + sl_a / 100), price * (1 - tp_a / 100)
        ev_a = eval_short(price, sl_price_a, tp_price_a, k5m_after, t)
        ev_ac = eval_short(price, sl_price_a, tp_price_a, k5m_after, t, sl_first=True)
        ev_b = eval_short(price, st["sl_price"], st["tp_price"], k5m_after, t)
        ev_bc = eval_short(price, st["sl_price"], st["tp_price"], k5m_after, t, sl_first=True)

        trades.append({
            "time": t, "ts": fmt_ts(t), "month": month_of(t), "price": price,
            "n_hit": n_hit, "n_total": n_total, "atr_pct": round(atr_pct, 2),
            "regime": regime_name, "high_vol": high_vol, "ret_30d": round(ret_30d, 2),
            "ret_a": ev_a["ret"], "sl_hit_a": ev_a["sl_hit"], "tp_hit_a": ev_a["tp_hit"],
            "ret_ac": ev_ac["ret"], "sl_hit_ac": ev_ac["sl_hit"], "tp_hit_ac": ev_ac["tp_hit"],
            "ret_b": ev_b["ret"], "sl_hit_b": ev_b["sl_hit"], "tp_hit_b": ev_b["tp_hit"],
            "ret_bc": ev_bc["ret"], "sl_hit_bc": ev_bc["sl_hit"], "tp_hit_bc": ev_bc["tp_hit"],
            "sl_pct_a": round(sl_a, 2), "tp_pct_a": round(tp_a, 2),
            "sl_pct_ac": round(sl_a, 2), "tp_pct_ac": round(tp_a, 2),
            "sl_pct_b": round(st["sl_pct"], 2), "tp_pct_b": round(st["tp_pct"], 2),
            "sl_pct_bc": round(st["sl_pct"], 2), "tp_pct_bc": round(st["tp_pct"], 2),
        })

    if not trades:
        print("❌ 无信号交易")
        sys.exit(1)

    rng = f"{fmt_ts(decision_ts[0])} ~ {fmt_ts(decision_ts[-1])}"
    print("\n" + "=" * 78)
    print(f"  4h做空系统【纯技术面降级】回测 | {symbol} | {days}天 | {rng}")
    print("=" * 78)
    print(f"  因子集: 10个纯技术因子(已剔除资金费率/OI) | 决策点 {len(decision_ts)} 个 | "
          f"信号 {len(trades)} 个 ({len(trades)/len(decision_ts)*100:.0f}%)")
    print(f"  方案A参数 SL={sl_mult}×ATR TP={tp_mult}×ATR | 每笔风险2% | $1000起始 | 最大持仓64h")

    NAMES = {"a": "ATR固定", "ac": "ATR固定(保守判SL)",
             "b": "结构止损", "bc": "结构止损(保守判SL)"}

    def show(title, rows, keys=("ac",)):
        if not rows:
            print(f"\n  {title}: 无样本")
            return
        for key in keys:
            s = simulate(rows, key)
            nm = NAMES.get(key, key)
            print(f"\n  {title} [{nm}]: n={s['n']} 胜率{s['win_rate']}% | avg {s['avg_ret']:+.3f}% | "
                  f"PF {s['profit_factor']} | SL{s['sl_hit']}%/TP{s['tp_hit']}% | "
                  f"总PnL ${s['total_pnl']:+.2f} ({s['total_ret_pct']:+.1f}%) 回撤{s['max_drawdown']}% | "
                  f"盈{s['avg_win']:+.2f}%/亏{s['avg_loss']:+.2f}%")

    print("\n" + "─" * 78)
    print("  【一】总体 — 主要看【保守判SL】口径（同一根内SL优先）")
    print("─" * 78)
    for th in (3, 4, 5):
        rows = [r for r in trades if r["n_hit"] >= th]
        show(f"n_hit≥{th}", rows, keys=("ac", "bc", "a"))

    print("\n" + "─" * 78)
    print("  【二】按 regime 分桶（ATR固定）")
    print("─" * 78)
    for reg in sorted({r["regime"] for r in trades}):
        rows = [r for r in trades if r["regime"] == reg]
        if len(rows) >= 3:
            show(f"regime={reg} (n={len(rows)})", rows)

    print("\n" + "─" * 78)
    print("  【三】按月度分桶（ATR固定）— 看逐月稳定性")
    print("─" * 78)
    for m in sorted({r["month"] for r in trades}):
        rows = [r for r in trades if r["month"] == m]
        show(f"{m} (n={len(rows)})", rows)

    print("\n" + "─" * 78)
    print("  【四】按「信号前30天中期趋势」分桶（ATR固定）— beta 归因")
    print("─" * 78)
    buckets = [("前30天上涨 >+5%", lambda r: r["ret_30d"] > 5),
               ("前30天小涨 +0~5%", lambda r: 0 < r["ret_30d"] <= 5),
               ("前30天下跌 -5~0%", lambda r: -5 <= r["ret_30d"] <= 0),
               ("前30天大跌 <-5%", lambda r: r["ret_30d"] < -5)]
    for nm, fn in buckets:
        show(nm, [r for r in trades if fn(r)])

    print("\n" + "─" * 78)
    print("  【五】体制闸门模拟：剔除 regime=TREND_UP 的信号（ATR固定）")
    print("─" * 78)
    show("全部信号", trades)
    show("剔除 TREND_UP 后", [r for r in trades if r["regime"] != "TREND_UP"])
    show("仅 TREND_UP", [r for r in trades if r["regime"] == "TREND_UP"])

    out_path = f"backtest_4h_tech_{symbol}_{days}d.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"symbol": symbol, "days": days, "mode": "tech-only",
                   "decision_range": [fmt_ts(decision_ts[0]), fmt_ts(decision_ts[-1])],
                   "params": {"sl_mult": sl_mult, "tp_mult": tp_mult},
                   "trades": trades}, f, ensure_ascii=False, indent=1)
    print(f"\n📁 明细已存: {out_path} (共 {len(trades)} 笔)")


if __name__ == "__main__":
    main()
