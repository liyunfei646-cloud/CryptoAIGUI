#!/usr/bin/env python3
"""
backtest_v2.py — V2.1 系统历史回测（无未来函数）
==================================================
对应《V2.0代码整改与V2.1策略研究实施说明》第 4/28/29/30/31/32/33/34/35/36/37/43 节。

用法:
  python3 backtest_v2.py [SYMBOL] [DAYS]
  python3 backtest_v2.py --experiment <name> [SYMBOL] [DAYS]
    experiment: baseline | no_whale | trend_only | reversal_only | range_only | breakout_only

V2.1 修复：
  - P0-2: 仅 signal_status == "READY" 的信号进入交易评估（WAIT 不算 LOSS）
  - P0-5: 回测与实时共用 analyzer.score_system（同一信号定义）
  - 同 K 触及 SL/TP → 保守 SL first（文档29节）
  - 交易成本：taker fee 0.05%×2 + slippage 0.02%×2 + funding 0.01%（文档28节）
  - 分组：LONG/SHORT / Strategy / Regime / Symbol / 置信度（文档32-35节）
  - 指标：Expectancy / PF / median / avg_win / avg_loss / MaxDD / MAE / MFE（文档30/31节）

方法：
  决策点 = 每根 1h K线收盘时刻（全部采样）
  每个决策点只用截至该时刻【已收盘】数据构造完整上下文（文档41/42节）
  评估：信号后 60m/4h 窗口，5m 粒度追踪 SL/TP/收益（价格收益，不含杠杆）
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

# V2.1: 交易成本（文档28节）— taker 0.05%×2 + slippage 0.02%×2 + funding 估算 0.01%
COST_PCT = 0.05 * 2 + 0.02 * 2 + 0.01

# 实验过滤（文档36/37节）
EXPERIMENTS = {
    "baseline": None,          # 全部策略
    "no_whale": None,          # whale 已在决策中移除，等同 baseline
    "trend_only": "TREND_CONTINUATION",
    "reversal_only": "LIQUIDITY_REVERSAL",
    "range_only": "RANGE_REVERSION",
    "breakout_only": "BREAKOUT",
}


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
        "funding_rate": None, "funding_level": None, "funding_series": [],
        "funding_trend": "flat", "funding_acceleration": 0.0,
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
            ctx["oi_change_15m"] = round(chg1h, 2)  # 回测降级：1h 粒度近似
            if chg1h > 1.0:
                ctx["oi_trend"] = "rising"
            elif chg1h < -1.0:
                ctx["oi_trend"] = "falling"
            else:
                ctx["oi_trend"] = "flat"
    # Funding（含 V2.1 level/trend/acceleration）
    fr_pts = [p for p in funding_hist if p["time"] <= t]
    if fr_pts:
        rates = [p["rate"] for p in fr_pts[-6:]]
        ctx["funding_rate"] = rates[-1]
        ctx["funding_level"] = rates[-1]
        ctx["funding_series"] = fr_pts[-6:]
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
        if len(rates) >= 2:
            ctx["funding_acceleration"] = rates[-1] - rates[-2]
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
    """
    5m 粒度追踪：60m/4h 双窗口。
    V2.1: 同 K 同时触及 SL/TP → 保守 SL first（文档29节）；含交易成本（文档28节）。
    返回收益(%)（已扣成本）与 SL/TP 命中。
    """
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
                # SL first：先检查止损（保守规则）
                if lo <= sl:
                    sl_hit = True
                    ret = (sl - entry) / entry * 100 - COST_PCT
                    break
                if hi >= tp:
                    tp_hit = True
                    ret = (tp - entry) / entry * 100 - COST_PCT
                    break
            else:
                mfe = max(mfe, (entry - lo) / entry * 100)
                mae = min(mae, (entry - hi) / entry * 100)
                if hi >= sl:
                    sl_hit = True
                    ret = (entry - sl) / entry * 100 - COST_PCT
                    break
                if lo <= tp:
                    tp_hit = True
                    ret = (entry - tp) / entry * 100 - COST_PCT
                    break
            last_close = k["close"]
        if not (sl_hit or tp_hit):
            ret = ((last_close - entry) / entry * 100 - COST_PCT) if direction == "LONG" \
                else ((entry - last_close) / entry * 100 - COST_PCT)
        out[label] = {"ret": ret, "mfe": mfe, "mae": mae, "sl_hit": sl_hit, "tp_hit": tp_hit}
    return out


def agg(rows: list[dict]) -> dict:
    """V2.1 指标集（文档30/31节）：胜率/均值/中位数/盈亏比/PF/Expectancy/MaxDD/MAE/MFE。"""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    rets = [r["ret_4h"] for r in rows]
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    wr = len(wins) / n
    expectancy = wr * aw - (1 - wr) * abs(al)
    # Max Drawdown（按时间顺序累计收益）
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in sorted(rows, key=lambda x: x["time"]):
        eq += r["ret_4h"]
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
    rets_sorted = sorted(rets)
    median_ret = rets_sorted[n // 2] if n % 2 else (rets_sorted[n // 2 - 1] + rets_sorted[n // 2]) / 2
    return {
        "n": n,
        "win_rate": round(wr * 100, 1),
        "avg_ret": round(sum(rets) / n, 3),
        "median_ret": round(median_ret, 3),
        "avg_win": round(aw, 3),
        "avg_loss": round(al, 3),
        "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
        "expectancy": round(expectancy, 3),
        "max_dd": round(max_dd, 2),
        "avg_mfe": round(sum(r["mfe_4h"] for r in rows) / n, 3),
        "avg_mae": round(sum(r["mae_4h"] for r in rows) / n, 3),
        "sl_hit": round(sum(1 for r in rows if r["sl_hit_4h"]) / n * 100, 1),
        "tp_hit": round(sum(1 for r in rows if r["tp_hit_4h"]) / n * 100, 1),
        "win_rate_60m": round(sum(1 for r in rows if r["ret_60m"] > 0) / n * 100, 1),
    }


def show(title: str, rows: list[dict]):
    a = agg(rows)
    if a["n"] == 0:
        print(f"\n  {title}: 无样本")
        return
    print(f"\n  {title}: n={a['n']}")
    print(f"    胜率(4h) {a['win_rate']}% | 60m胜率 {a['win_rate_60m']}% | avg {a['avg_ret']:+.3f}% | median {a['median_ret']:+.3f}%")
    print(f"    avg_win {a['avg_win']:+.3f}% | avg_loss {a['avg_loss']:+.3f}% | PF {a['profit_factor']} | Expectancy {a['expectancy']:+.3f}%")
    print(f"    MaxDD {a['max_dd']}% | MFE {a['avg_mfe']:.2f}% | MAE {a['avg_mae']:.2f}% | SL {a['sl_hit']}% | TP {a['tp_hit']}%")


def main():
    args = sys.argv[1:]
    experiment = "baseline"
    if args and args[0].startswith("--experiment"):
        experiment = args[1] if len(args) > 1 else "baseline"
        args = args[2:]
        if experiment not in EXPERIMENTS:
            print(f"❌ 未知实验: {experiment}，可选: {list(EXPERIMENTS)}")
            return
    symbol = args[0].upper() if args else "BTCUSDT"
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    days = int(args[1]) if len(args) > 1 else 20
    strat_filter = EXPERIMENTS[experiment]

    print(f"⏳ 拉取历史数据 {symbol} ({days}天) [实验: {experiment}]...", flush=True)
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
    trades = []       # READY 信号（进入交易评估）
    skipped = []      # 非 READY 决策点（WAIT/NO_TRADE，统计分布但不评估）

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

        status = score.get("signal_status", "WAIT")
        cand = score.get("candidate_direction", "NEUTRAL")
        strategy = score.get("strategy", "NONE")
        # 实验过滤（文档37节）：只保留指定策略的 READY 信号
        if status == "READY" and strat_filter and strategy != strat_filter:
            status = "WAIT"  # 被实验过滤 → 统计为跳过

        base_row = {
            "time": t, "ts": fmt_ts(t), "price": price,
            "regime": score.get("market_regime"), "grade": score.get("grade"),
            "strategy": strategy, "setup": score.get("setup"),
            "candidate_direction": cand,
            "signal_status": status,
            "counter_trend": bool(score.get("counter_trend")),
            "confidence": score.get("score_confidence", 0),
            "need_confirm": score.get("need_confirm", 0),
            "confirm_count": score.get("confirm_count", 0),
        }

        if status != "READY":
            skipped.append(base_row)
            continue

        trade_direction = score.get("trade_direction", "NEUTRAL")
        if trade_direction not in ("LONG", "SHORT"):
            skipped.append(base_row)
            continue
        try:
            risk = risk_recommendation(price, score, 1000)
        except Exception:
            continue
        entry, sl, tp = risk["entry_price"], risk["stop_loss"], risk["take_profit"]
        if not (entry and sl and tp):
            skipped.append(base_row)
            continue
        # 评估：决策点后 5m 已收盘 K 线
        k5m_after = [k for k in k5m_by_time if k["close_time"] > t]
        ev = evaluate_trade(entry, sl, tp, trade_direction, k5m_after, t)

        row = dict(base_row)
        row.update({
            "direction": trade_direction,
            "entry": entry, "sl": sl, "tp": tp,
            "sl_pct": risk["sl_pct"], "tp_pct": risk["tp_pct"],
            "rr": risk["rr_ratio"], "sl_basis": risk.get("sl_basis"),
            "ret_4h": ev["4h"]["ret"], "mfe_4h": ev["4h"]["mfe"], "mae_4h": ev["4h"]["mae"],
            "sl_hit_4h": ev["4h"]["sl_hit"], "tp_hit_4h": ev["4h"]["tp_hit"],
            "ret_60m": ev["60m"]["ret"],
        })
        trades.append(row)

    # ── 报告 ──
    print("\n" + "=" * 70)
    print(f"  V2.1 回测报告 | {symbol} | {days}天 | 实验: {experiment} | "
          f"{fmt_ts(decision_ts[0])} ~ {fmt_ts(decision_ts[-1])}")
    print("=" * 70)
    print(f"  决策点: {len(decision_ts)} | READY交易: {len(trades)} | 跳过(WAIT/NO_TRADE/过滤): {len(skipped)}")
    print(f"  成本: {COST_PCT:.2f}%/笔 (fee+slip+funding) | SL/TP 同K保守SL first")

    # 跳过分布
    if skipped:
        from collections import Counter
        st_cnt = Counter(r["signal_status"] for r in skipped)
        print(f"  跳过分布: {dict(st_cnt)}")
        if strat_filter:
            print(f"  (实验 {experiment} 过滤掉非 {strat_filter} 的 READY 信号)")

    show("全部 READY 交易", trades)

    for d in ("LONG", "SHORT"):
        show(f"按方向 [{d}]", [r for r in trades if r["direction"] == d])

    # Strategy 分组（文档35节）
    print("\n  ── Strategy × Direction ──")
    for st in sorted({r["strategy"] for r in trades}):
        for d in ("LONG", "SHORT"):
            rows = [r for r in trades if r["strategy"] == st and r["direction"] == d]
            if rows:
                show(f"  {st} + {d}", rows)

    # Regime 分组（文档34节）
    print("\n  ── Regime × Direction ──")
    for reg in sorted({r["regime"] for r in trades}):
        for d in ("LONG", "SHORT"):
            rows = [r for r in trades if r["regime"] == reg and r["direction"] == d]
            if rows:
                show(f"  {reg} + {d}", rows)

    # 顺势 / 逆势（文档19节）
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
    for lo, hi in ((0, 60), (60, 70), (70, 80), (80, 101)):
        show(f"  置信度 {lo}-{hi}%", [r for r in trades if lo <= r["confidence"] < hi])

    # 落盘
    out_path = f"backtest_{symbol}_{days}d_{experiment}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"symbol": symbol, "days": days, "experiment": experiment,
                   "decision_ts": [fmt_ts(x) for x in (decision_ts[0], decision_ts[-1])],
                   "cost_pct": COST_PCT,
                   "trades": trades, "skipped": skipped}, f, ensure_ascii=False, indent=1)
    print(f"\n📁 明细已存: {out_path} (READY {len(trades)} 条 / 跳过 {len(skipped)} 条)")


if __name__ == "__main__":
    main()
