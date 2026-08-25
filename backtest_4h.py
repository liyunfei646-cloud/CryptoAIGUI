#!/usr/bin/env python3
"""
backtest_4h.py — 4h做空系统完整 SL/TP 盈亏曲线回测（无未来函数）
=================================================================
背景：2026-07-31 已验证 4h 做空【方向命中率】为正期望（SPCX 71.6%，
五币做空 5/5 正收益），但完整 SL/TP 盈亏曲线从未回测。
2026-08-25：移植 V2.0 验证过的组件（结构止损+动态TP、Market Regime），
本脚本对两种 SL/TP 方案做等条件对照回测。

用法: python3 backtest_4h.py [SYMBOL] [DAYS]
默认: SPCXUSDT 20 天（衍生品历史覆盖上限 ~20.8 天，OI 1h limit=500）

方法：
  决策点 = 每根 4h K线收盘时刻（最近 N 天）
  每个决策点只用截至该时刻【已收盘】数据：
    - 4h 切片（≥60 根）→ compute_short_factors（funding/OI 注入历史值）
    - 1h 切片 → detect_market_regime（V2.0 移植）
  评估：信号后 5m 粒度追踪，最大持仓 16 根 4h（≈2.7天）
    - 方案A: ATR 固定 SL=max(ATR×1.5,1)% / TP=ATR×3%（现状）
    - 方案B: 结构止损+动态TP（V2.0 移植：SL=结构高点×1.001 vs ATR×1.0 取大
             上限 ATR×3 下限 0.5%；TP1=最近支撑；TP2=24h低）
  组合模拟：$1000 起始，每笔固定风险 2%（按 SL% 反推仓位），不含杠杆

输出：方案A/B 胜率/avg_ret/盈亏比/总收益/最大回撤 + regime 分桶 + n_hit 分桶
"""
import json
import sys
import time
from datetime import datetime, timezone

from analyzer import fetch_klines, find_swing_points
from futures_data import fetch_oi_series, fetch_funding_series, detect_market_regime
from coin_analysis_4h import compute_short_factors, structural_sl_tp, MIN_FACTORS

TZ = timezone.utc
MAX_HOLD_MS = 16 * 4 * 3600_000  # 16根4h = 64h
RISK_PER_TRADE = 0.02            # 每笔风险 2%
START_BALANCE = 1000.0


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%m-%d %H:%M")


def fetch_history(symbol: str, interval: str, days: int, limit: int = 1000) -> list[dict]:
    """分页拉历史 K 线（升序）。"""
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
        time.sleep(0.15)
    return out


def slice_before(klines: list[dict], t: int, n: int) -> list[dict]:
    return [k for k in klines if k["close_time"] <= t][-n:]


def funding_at(t: int, funding_hist: list[dict]) -> float:
    """决策点 t 的最近 funding rate。"""
    for p in reversed(funding_hist):
        if p["time"] <= t:
            return p["rate"]
    return 0.0


def oi_series_at(t: int, oi_hist: list[dict], n: int = 9) -> list[float]:
    """决策点 t 的 OI 序列（由旧到新，≤n 个）。"""
    return [p["oi_value"] for p in oi_hist if p["time"] <= t][-n:]


def evaluate_short(entry: float, sl: float, tp: float, k5m_after: list[dict], t: int,
                   tp2: float = None) -> dict:
    """5m 粒度追踪 SHORT：先触 TP 算 TP 命中（与 V2.0 evaluate_trade 同约定）。
    最大持仓 64h。返回 ret(%) / sl_hit / tp_hit / mfe / mae / exit_price。"""
    end_t = t + MAX_HOLD_MS
    mfe = mae = 0.0
    ret = 0.0
    sl_hit = tp_hit = False
    last_close = entry
    for k in k5m_after:
        if k["close_time"] > end_t:
            break
        hi, lo = k["high"], k["low"]
        mfe = max(mfe, (entry - lo) / entry * 100)
        mae = min(mae, (entry - hi) / entry * 100)
        if lo <= tp:
            tp_hit = True
            ret = (entry - tp) / entry * 100
            break
        if hi >= sl:
            sl_hit = True
            ret = (entry - sl) / entry * 100  # SHORT 止损在上方，亏损为负
            break
        last_close = k["close"]
    if not (sl_hit or tp_hit):
        ret = (entry - last_close) / entry * 100
    return {"ret": ret, "mfe": mfe, "mae": mae, "sl_hit": sl_hit, "tp_hit": tp_hit,
            "exit_price": last_close}


def simulate(rows: list[dict], key: str) -> dict:
    """按 rows 中每笔的 ret（价格收益%）模拟组合：每笔风险 2%。"""
    bal = START_BALANCE
    peak = START_BALANCE
    max_dd = 0.0
    rets = []
    for r in rows:
        sl_pct = r[f"sl_pct_{key}"]
        if sl_pct <= 0:
            continue
        pos = bal * RISK_PER_TRADE / (sl_pct / 100)
        pnl = pos * r[f"ret_{key}"] / 100
        bal += pnl
        rets.append(pnl)
        peak = max(peak, bal)
        max_dd = max(max_dd, (peak - bal) / peak * 100)
    wins = sum(1 for r in rows if r[f"ret_{key}"] > 0)
    n = len(rows)
    gross = sum(r[f"ret_{key}"] for r in rows)
    return {
        "n": n,
        "win_rate": round(wins / n * 100, 1) if n else 0,
        "avg_ret": round(gross / n, 3) if n else 0,
        "total_ret_pct": round(sum(r[f"ret_{key}"] for r in rows), 2),
        "sl_hit": round(sum(1 for r in rows if r[f"sl_hit_{key}"]) / n * 100, 1) if n else 0,
        "tp_hit": round(sum(1 for r in rows if r[f"tp_hit_{key}"]) / n * 100, 1) if n else 0,
        "avg_win": round(sum(r[f"ret_{key}"] for r in rows if r[f"ret_{key}"] > 0) /
                         max(1, sum(1 for r in rows if r[f"ret_{key}"] > 0)), 3),
        "avg_loss": round(sum(r[f"ret_{key}"] for r in rows if r[f"ret_{key}"] <= 0) /
                          max(1, sum(1 for r in rows if r[f"ret_{key}"] <= 0)), 3),
        "profit_factor": round(sum(r[f"ret_{key}"] for r in rows if r[f"ret_{key}"] > 0) /
                               abs(sum(r[f"ret_{key}"] for r in rows if r[f"ret_{key}"] <= 0)), 2)
                               if sum(r[f"ret_{key}"] for r in rows if r[f"ret_{key}"] <= 0) != 0 else float("inf"),
        "final_balance": round(bal, 2),
        "total_pnl": round(bal - START_BALANCE, 2),
        "max_drawdown": round(max_dd, 2),
    }


def main():
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "SPCXUSDT"
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    sl_mult = float(sys.argv[3]) if len(sys.argv) > 3 else 1.5
    tp_mult = float(sys.argv[4]) if len(sys.argv) > 4 else 3.0

    print(f"⏳ 拉取历史数据 {symbol} ({days}天)...", flush=True)
    t0 = time.time()
    k4h = fetch_history(symbol, "4h", days + 15)
    k1h = fetch_history(symbol, "1h", days + 15)
    k5m = fetch_history(symbol, "5m", days + 2)
    oi_hist = fetch_oi_series(symbol, "1h", 500) or []
    funding_hist = fetch_funding_series(symbol, 500) or []
    print(f"✅ 数据就绪: 4h×{len(k4h)} 1h×{len(k1h)} 5m×{len(k5m)} "
          f"OI×{len(oi_hist)} Funding×{len(funding_hist)} ({time.time()-t0:.0f}s)", flush=True)
    if not k4h or not k5m or not oi_hist:
        print("❌ 数据不足")
        sys.exit(1)

    # 决策区间：OI 历史覆盖 ∩ 4h 数据覆盖
    deriv_end = oi_hist[-1]["time"]
    deriv_start = max(oi_hist[0]["time"], k4h[0]["close_time"] + 60 * 4 * 3600_000)
    decision_ts = sorted(k["close_time"] for k in k4h
                         if deriv_start <= k["close_time"] <= deriv_end)
    print(f"决策点: {len(decision_ts)} 个 (区间 {fmt_ts(decision_ts[0])} ~ {fmt_ts(decision_ts[-1])})", flush=True)

    trades = []
    for i, t in enumerate(decision_ts):
        if i % 30 == 0:
            print(f"  ... {i}/{len(decision_ts)} ({fmt_ts(t)})", flush=True)

        c4h = slice_before(k4h, t, 300)
        c1h = slice_before(k1h, t, 200)
        if len(c4h) < 60:
            continue

        # 信号（注入历史 funding / OI，禁止实时调用）
        try:
            sig = compute_short_factors(
                c4h, interval_minutes=240, symbol=symbol,
                funding_rate_override=funding_at(t, funding_hist),
                oi_series_override=oi_series_at(t, oi_hist, 9))
        except Exception:
            continue
        n_hit = sig["n_hit"]
        if n_hit < MIN_FACTORS:
            continue  # 无做空信号

        price = sig["price"]
        atr_pct = sig["atr_pct"]
        conf = sig["confidence"]
        swing = find_swing_points(c4h, 5)
        try:
            regime = detect_market_regime(c4h, c1h)
            regime_name = regime.get("regime", "CHAOS")
            high_vol = regime.get("high_volatility", False)
        except Exception:
            regime_name, high_vol = "CHAOS", False

        # 方案A：ATR 固定（可配 SL/TP 乘数）
        sl_a = max(atr_pct * sl_mult, 1.0) if sl_mult >= 1.0 else atr_pct * sl_mult
        tp_a = atr_pct * tp_mult
        # 方案B：结构止损 + 动态TP
        st = structural_sl_tp(price, atr_pct, c4h, swing["swing_highs"], swing["swing_lows"])

        k5m_after = [k for k in k5m if k["close_time"] > t]
        ev_a = evaluate_short(price, price * (1 + sl_a / 100), price * (1 - tp_a / 100), k5m_after, t)
        ev_b = evaluate_short(price, st["sl_price"], st["tp_price"], k5m_after, t, st["tp2_price"])

        trades.append({
            "time": t, "ts": fmt_ts(t), "price": price, "n_hit": n_hit, "conf": round(conf, 1),
            "atr_pct": round(atr_pct, 2), "regime": regime_name, "high_vol": high_vol,
            "sl_basis_b": st["sl_basis"],
            "ret_a": ev_a["ret"], "sl_hit_a": ev_a["sl_hit"], "tp_hit_a": ev_a["tp_hit"],
            "ret_b": ev_b["ret"], "sl_hit_b": ev_b["sl_hit"], "tp_hit_b": ev_b["tp_hit"],
            "sl_pct_a": round(sl_a, 2), "tp_pct_a": round(tp_a, 2),
            "sl_pct_b": round(st["sl_pct"], 2), "tp_pct_b": round(st["tp_pct"], 2),
        })

    if not trades:
        print("❌ 无信号交易")
        sys.exit(1)

    # ── 报告 ──
    print("\n" + "=" * 74)
    print(f"  4h做空系统 SL/TP 回测 | {symbol} | {days}天 | {fmt_ts(decision_ts[0])} ~ {fmt_ts(decision_ts[-1])}")
    print("=" * 74)
    print(f"  信号数(n_hit≥{MIN_FACTORS}): {len(trades)} | 决策点: {len(decision_ts)} | "
          f"信号率 {len(trades)/len(decision_ts)*100:.0f}% | 方案A参数: SL={sl_mult}×ATR TP={tp_mult}×ATR")
    print(f"  每笔风险 2%，$1000 起始，不含杠杆；最大持仓 64h（16根4h）")

    def show(title, rows, keys=("a", "b")):
        if not rows:
            print(f"\n  {title}: 无样本")
            return
        for key in keys:
            s = simulate(rows, key)
            name = "ATR固定" if key == "a" else "结构止损"
            print(f"\n  {title} [{name}]: n={s['n']} 胜率{s['win_rate']}% | "
                  f"avg {s['avg_ret']:+.3f}% | PF {s['profit_factor']} | "
                  f"SL{s['sl_hit']}%/TP{s['tp_hit']}% | 总PnL ${s['total_pnl']:+.2f} "
                  f"({s['total_ret_pct']:+.1f}%) 回撤{s['max_drawdown']}% | 均值 盈{s['avg_win']:+.2f}%/亏{s['avg_loss']:+.2f}%")

    show("全部做空信号", trades)
    for reg in sorted({r["regime"] for r in trades}):
        rows = [r for r in trades if r["regime"] == reg]
        if len(rows) >= 3:
            show(f"regime={reg} (n={len(rows)})", rows)

    print("\n  ── n_hit 分桶（结构止损方案）──")
    for lo, hi in ((4, 5), (5, 7), (7, 99)):
        rows = [r for r in trades if lo <= r["n_hit"] < hi]
        if rows:
            show(f"  n_hit {lo}-{hi} (n={len(rows)})", rows, keys=("b",))
    show(f"  n_hit ≥7", [r for r in trades if r["n_hit"] >= 7], keys=("b",))

    print("\n  ── 高波动过滤对照（结构止损）──")
    show("  high_vol=True", [r for r in trades if r["high_vol"]], keys=("b",))
    show("  high_vol=False", [r for r in trades if not r["high_vol"]], keys=("b",))

    # 落盘
    out_path = f"backtest_4h_{symbol}_{days}d.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"symbol": symbol, "days": days,
                   "decision_range": [fmt_ts(decision_ts[0]), fmt_ts(decision_ts[-1])],
                   "trades": trades}, f, ensure_ascii=False, indent=1)
    print(f"\n📁 明细已存: {out_path} (共 {len(trades)} 笔)")


if __name__ == "__main__":
    main()
