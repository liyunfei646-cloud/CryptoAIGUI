#!/usr/bin/env python3
"""
backtest_v31.py — V3.1 Alpha 验证框架（无未来函数）
================================================================
按《2026-09-24 V3.1 回测验证方案》实现：

  P0-1 交易成本模型（分档 + 生命周期 + Funding 事件数）
  P0-2 Direction Randomization Engine
        Mode A  Independent Random（每笔独立随机方向）
        Mode B  Permutation（保多空比例重排；方向单一时自动退化）
        Mode B' Timing Permutation（纯做空策略的替代零假设：随机入场时点）
  P0-3 Baseline：Trend / Buy&Hold / Short&Hold(Inverse)
  P0-4 Walk-Forward（训练30天 / 测试15天 / 滚动15天）+ OOS 汇总
  P0-5 Cost Sensitivity（乐观 / 基准 / 保守）
  P1   统计显著性（Bootstrap 95% CI on Net PF）+ Regime 分桶

核心口径：
  - 决策点 = 每根 4h 收盘；只用截至该时刻已收盘数据
  - 4h 做空因子集（derivatives_disabled=True，10 纯技术因子，覆盖 90 天）
  - 5m 粒度追踪，最大持仓 64h；同一根内 SL/TP 都触及 → 判 SL（保守）
  - 成本按交易生命周期扣：入场 half-spread+slip → 手续费 → Funding 事件 → 出场
  - Net PF 为核心指标；Gross PF 仅作对照

用法: python3 backtest_v31.py [SYMBOLS] [DAYS]
默认: BTCUSDT,ETHUSDT,SOLUSDT,SPCXUSDT 90 天
"""
import bisect
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timezone

from analyzer import find_swing_points
from futures_data import detect_market_regime, fetch_funding_series
from coin_analysis_4h import compute_short_factors, MIN_FACTORS
from backtest_4h import fetch_history, slice_before, MAX_HOLD_MS, MIN_TECH_FACTORS

TZ = timezone.utc
WARMUP_BARS = 200
RISK_PER_TRADE = 0.02
START_BALANCE = 1000.0
N_MC = 500           # Randomization / Bootstrap 次数
MIN_TRAIN_TRADES = 5
MIN_SIG = int(os.environ.get("MIN_SIG", MIN_TECH_FACTORS))   # 信号因子门槛（生产=4）
SPAN_MODE = os.environ.get("SPAN_MODE", "exact")             # exact=严格days窗 / legacy=旧口径
SUFFIX = f"_sig{MIN_SIG}" + ("_legacy" if SPAN_MODE == "legacy" else "")

# ── 成本分档（bp，1bp = 0.01%）─────────────────────────────────
TIER1 = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT"}
TIER2 = {"SOLUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT"}
PRESETS = {
    # 乐观：VIP/低费率 + 主流盘口
    "optimistic":   {"fee_per_side": 0.0002, "t1": (0.5, 0.5), "t2": (1.5, 1.0), "t3": (8.0, 4.0)},
    # 基准：Taker 0.05%/边
    "base":         {"fee_per_side": 0.0005, "t1": (1.0, 1.0), "t2": (3.0, 2.0), "t3": (15.0, 8.0)},
    # 保守：高费率 + 低流动性币盘口恶化
    "conservative": {"fee_per_side": 0.0007, "t1": (2.0, 2.0), "t2": (6.0, 4.0), "t3": (30.0, 16.0)},
}


def tier_of(symbol: str) -> str:
    if symbol in TIER1:
        return "t1"
    if symbol in TIER2:
        return "t2"
    return "t3"


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M")


def day_key(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d")


# ── 成本模型 ───────────────────────────────────────────────────
def cost_net(gross_ret, entry, exit_px, direction, tier, scenario, funding_sum):
    """生命周期成本：入场/出场 half-spread+slip + 双边手续费 + Funding 事件。
    direction: +1 LONG / -1 SHORT。gross_ret 为价格收益%（已含方向）。
    返回 (net_ret_pct, 成本明细 dict)。"""
    p = PRESETS[scenario]
    spread_bp, slip_bp = p[tier]
    fee = p["fee_per_side"]
    hs = (spread_bp / 2.0 + slip_bp) / 10000.0     # 单边冲击（小数）

    if direction < 0:      # SHORT: 卖出开仓，买回平仓
        entry_eff = entry * (1 - hs)
        exit_eff = exit_px * (1 + hs)
        gross_eff = (entry_eff - exit_eff) / entry_eff * 100
    else:                  # LONG: 买入开仓，卖出平仓
        entry_eff = entry * (1 + hs)
        exit_eff = exit_px * (1 - hs)
        gross_eff = (exit_eff - entry_eff) / entry_eff * 100

    fee_pct = fee * 2 * 100                        # 双边，转 %
    fund_pct = -direction * funding_sum * 100      # SHORT 收正费率，LONG 付
    net = gross_eff - fee_pct + fund_pct
    return net, {
        "gross_eff": round(gross_eff, 4),
        "fee_pct": round(fee_pct, 4),
        "fund_pct": round(fund_pct, 4),
        "impact_bp": round((spread_bp / 2.0 + slip_bp) * 2, 2),
    }


def funding_between(fund_hist, t0, t1, ):
    """持仓区间内的 Funding 事件之和（每 8h 一次）。"""
    if not fund_hist:
        return 0.0
    s = 0.0
    for f in fund_hist:
        if t0 < f["time"] <= t1:
            s += f["rate"]
    return s


# ── 单笔路径评估 ───────────────────────────────────────────────
def eval_path(path, entry, direction, sl_pct, tp_pct, t0):
    """5m 粒度追踪。同一根内 SL/TP 都触及 → 判 SL（保守）。
    返回 dict(exit_px, exit_time, reason, gross_ret, bars)"""
    if direction < 0:
        sl_px, tp_px = entry * (1 + sl_pct / 100), entry * (1 - tp_pct / 100)
    else:
        sl_px, tp_px = entry * (1 - sl_pct / 100), entry * (1 + tp_pct / 100)

    end_t = t0 + MAX_HOLD_MS
    last_close, last_t = entry, t0
    for k in path:
        if k["close_time"] > end_t:
            break
        hi, lo = k["high"], k["low"]
        if direction < 0:
            sl_now, tp_now = hi >= sl_px, lo <= tp_px
        else:
            sl_now, tp_now = lo <= sl_px, hi >= tp_px
        if sl_now:
            return {"exit_px": sl_px, "exit_time": k["close_time"], "reason": "SL",
                    "gross_ret": direction * (sl_px - entry) / entry * 100}
        if tp_now:
            return {"exit_px": tp_px, "exit_time": k["close_time"], "reason": "TP",
                    "gross_ret": direction * (tp_px - entry) / entry * 100}
        last_close, last_t = k["close"], k["close_time"]
    return {"exit_px": last_close, "exit_time": last_t, "reason": "TIME",
            "gross_ret": direction * (last_close - entry) / entry * 100}


# ── 统计 ───────────────────────────────────────────────────────
def metrics(rows, key="net_ret"):
    rets = [r[key] for r in rows]
    n = len(rets)
    if n == 0:
        return {"n": 0}
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    gp, gl = sum(wins), abs(sum(losses))
    pf = (gp / gl) if gl > 1e-12 else float("inf")

    bal, peak, max_dd = START_BALANCE, START_BALANCE, 0.0
    for r in rows:
        slp = r.get("sl_pct") or 1.0
        pos = bal * RISK_PER_TRADE / (slp / 100.0)
        bal += pos * r[key] / 100.0
        peak = max(peak, bal)
        max_dd = max(max_dd, (peak - bal) / peak * 100)
    mean = statistics.fmean(rets)
    sd = statistics.pstdev(rets) if n > 1 else 0.0
    return {
        "n": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "avg_ret": round(mean, 4),
        "expectancy": round(mean, 4),
        "sd": round(sd, 4),
        "pf": round(pf, 3) if pf != float("inf") else None,
        "gross_pf": None,
        "total_ret_pct": round(sum(rets), 2),
        "sharpe_per_trade": round(mean / sd, 3) if sd > 0 else None,
        "sharpe_annual": round(mean / sd * (n ** 0.5) * (365 / 90) ** 0.5, 2) if sd > 0 else None,
        "max_dd_pct": round(max_dd, 2),
        "final_balance": round(bal, 2),
    }


def with_gross(rows, m):
    """在同一 rows 上补 Gross PF（价格收益，未扣成本）。"""
    g = [r["gross_ret"] for r in rows]
    if not g:
        m["gross_pf"] = None
        m["gross_avg_ret"] = None
        return m
    gp = sum(x for x in g if x > 0)
    gl = abs(sum(x for x in g if x <= 0))
    m["gross_pf"] = round(gp / gl, 3) if gl > 1e-12 else None
    m["gross_avg_ret"] = round(statistics.fmean(g), 4)
    return m


def bootstrap_pf_ci(rows, key="net_ret", n_boot=2000, seed=7):
    rets = [r[key] for r in rows]
    n = len(rets)
    if n < 5:
        return None, None
    rnd = random.Random(seed)
    pfs = []
    for _ in range(n_boot):
        s = [rets[rnd.randrange(n)] for _ in range(n)]
        gp = sum(x for x in s if x > 0)
        gl = abs(sum(x for x in s if x <= 0))
        if gl > 1e-12:
            pfs.append(gp / gl)
    if not pfs:
        return None, None
    pfs.sort()
    return round(pfs[int(0.025 * len(pfs))], 3), round(pfs[int(0.975 * len(pfs))], 3)


def line(m, label="", extra=""):
    if not m or m.get("n", 0) == 0:
        return f"  {label}: 无样本"
    pf = f"{m['pf']:.2f}" if m["pf"] is not None else "inf"
    gpf = f"{m['gross_pf']:.2f}" if m.get("gross_pf") is not None else "-"
    return (f"  {label}: n={m['n']} 胜率{m['win_rate']}% | avg {m['avg_ret']:+.4f}% | "
            f"GrossPF {gpf} → NetPF {pf} | 总{m['total_ret_pct']:+.2f}% | "
            f"回撤{m['max_dd_pct']}% | Sharpe(t) {m['sharpe_per_trade']} {extra}")


# ═══════════════════════════════════════════════════════════════
def run_symbol(symbol: str, days: int) -> dict:
    print(f"\n{'='*80}\n【{symbol}】拉取 {days}天 数据（含 {WARMUP_BARS}根4h 预热）\n{'='*80}", flush=True)
    t0 = time.time()
    k4h = fetch_history(symbol, "4h", days + 60)
    k1h = fetch_history(symbol, "1h", days + 60)
    k5m = fetch_history(symbol, "5m", days + 3)
    funding = fetch_funding_series(symbol, 1000) or []
    print(f"  数据: 4h×{len(k4h)} 1h×{len(k1h)} 5m×{len(k5m)} Funding×{len(funding)} ({time.time()-t0:.0f}s)", flush=True)
    if len(k4h) <= WARMUP_BARS + 5 or not k5m or not funding:
        print("  ❌ 数据不足")
        return {}

    m5_times = [k["close_time"] for k in k5m]

    def path_after(t):
        i = bisect.bisect_right(m5_times, t)
        return k5m[i:]

    now_ms = int(time.time() * 1000)
    cut = now_ms - MAX_HOLD_MS
    start_ms = cut - days * 86400_000
    if SPAN_MODE == "legacy":   # 旧口径：不按 days 截窗（实际覆盖 days+27 天）
        decision = [k for i, k in enumerate(k4h)
                    if i >= WARMUP_BARS and k["close_time"] <= cut]
    else:
        decision = [k for i, k in enumerate(k4h)
                    if i >= WARMUP_BARS and start_ms <= k["close_time"] <= cut]
    print(f"  决策点: {len(decision)} 个 ({fmt_ts(decision[0]['close_time'])} ~ {fmt_ts(decision[-1]['close_time'])})", flush=True)

    # ── 候选池（信号 + regime + ATR），带缓存评估 ──
    cand = []
    for i, k in enumerate(decision):
        if i % 100 == 0:
            print(f"    ... 扫描 {i}/{len(decision)}", flush=True)
        t = k["close_time"]
        c4h = slice_before(k4h, t, 400)
        c1h = slice_before(k1h, t, 200)
        if len(c4h) < WARMUP_BARS:
            continue
        try:
            sig = compute_short_factors(c4h, interval_minutes=240, symbol=symbol,
                                        derivatives_disabled=True)
        except Exception:
            continue
        try:
            reg = detect_market_regime(c4h, c1h)
            regime = reg.get("regime", "CHAOS")
        except Exception:
            regime = "CHAOS"
        cand.append({"i": len(cand), "t": t, "price": sig["price"], "atr_pct": sig["atr_pct"],
                     "n_hit": sig["n_hit"], "n_total": sig["n_total"], "regime": regime,
                     "path": path_after(t)})

    print(f"  候选: {len(cand)} 个（n_hit≥{MIN_SIG}: "
          f"{sum(1 for c in cand if c['n_hit'] >= MIN_SIG)}）", flush=True)

    tier = tier_of(symbol)
    cache = {}

    def build_trade(c, direction, sl_mult=1.0, tp_mult=1.0, scenario="base",
                    sl_pct=None, tp_pct=None):
        """构造一笔交易（含成本）。direction: +1/-1"""
        if sl_pct is None:
            sl_pct = max(c["atr_pct"] * sl_mult, 1.0) if sl_mult >= 1.0 else c["atr_pct"] * sl_mult
        if tp_pct is None:
            tp_pct = c["atr_pct"] * tp_mult
        ck = (c["i"], direction, round(sl_pct, 6), round(tp_pct, 6))
        if ck not in cache:
            ev = eval_path(c["path"], c["price"], direction, sl_pct, tp_pct, c["t"])
            cache[ck] = (sl_pct, tp_pct, ev)
        sl_pct, tp_pct, ev = cache[ck]
        fs = funding_between(funding, c["t"], ev["exit_time"])
        net, det = cost_net(ev["gross_ret"], c["price"], ev["exit_px"], direction,
                            tier, scenario, fs)
        return {
            "t": c["t"], "ts": fmt_ts(c["t"]), "day": day_key(c["t"]),
            "price": c["price"], "atr_pct": c["atr_pct"], "n_hit": c["n_hit"],
            "regime": c["regime"], "dir": direction, "sl_pct": sl_pct, "tp_pct": tp_pct,
            "exit_time": ev["exit_time"], "reason": ev["reason"],
            "gross_ret": round(ev["gross_ret"], 4), "net_ret": round(net, 4),
            "funding_sum": round(fs, 6), "cost": det,
            "hold_h": round((ev["exit_time"] - c["t"]) / 3600000, 1),
        }

    # ── P0-4 Walk-Forward：训练30天 / 测试15天 / 滚动15天 ──
    D = 86400_000
    ts_list = [c["t"] for c in cand]
    start, end = ts_list[0], ts_list[-1]
    grid = [(sm, tm) for sm in (0.5, 1.0, 1.5) for tm in (0.5, 1.0, 2.0)]
    minfac_grid = (MIN_SIG, MIN_SIG + 1)

    windows, oos_wf = [], []
    w_start = start
    while w_start + 45 * D <= end:
        tr0, tr1 = w_start, w_start + 30 * D
        te0, te1 = tr1, w_start + 45 * D
        train_c = [c for c in cand if tr0 <= c["t"] < tr1]
        test_c = [c for c in cand if te0 <= c["t"] < te1]

        best = None
        for sm, tm in grid:
            for mf in minfac_grid:
                rows = [build_trade(c, -1, sm, tm) for c in train_c if c["n_hit"] >= mf]
                if len(rows) < MIN_TRAIN_TRADES:
                    continue
                m = metrics(rows)
                score = m["pf"] if (m["pf"] is not None and m["avg_ret"] > 0) else 0
                if best is None or score > best[0]:
                    best = (score, sm, tm, mf, m)
        sel = (best[1], best[2], best[3]) if best else (1.0, 1.0, MIN_SIG)
        win_rows = [build_trade(c, -1, sel[0], sel[1]) for c in test_c if c["n_hit"] >= sel[2]]
        if len(win_rows) < 3:      # 选中参数在测试窗样本过少 → 回退生产默认
            sel = (1.0, 1.0, MIN_SIG)
            win_rows = [build_trade(c, -1, sel[0], sel[1]) for c in test_c if c["n_hit"] >= sel[2]]
        m = with_gross(win_rows, metrics(win_rows))
        windows.append({"train": [fmt_ts(tr0), fmt_ts(tr1)], "test": [fmt_ts(te0), fmt_ts(te1)],
                        "params": {"sl": sel[0], "tp": sel[1], "min_factors": sel[2]},
                        "train_metrics": best[4] if best else None, "test_metrics": m})
        oos_wf.extend(win_rows)
        w_start += 15 * D

    m_wf = with_gross(oos_wf, metrics(oos_wf))
    wf_ci = bootstrap_pf_ci(oos_wf)

    # ── OOS-Default：生产参数（1.0×ATR / 1.0×ATR / 因子≥3），不做任何拟合 ──
    oos_range = ((windows[0]["test"][0], windows[-1]["test"][1]) if windows
                 else (fmt_ts(start), fmt_ts(end)))
    oos_c = [c for c in cand if c["n_hit"] >= MIN_SIG
             and oos_range[0] <= fmt_ts(c["t"]) < oos_range[1]]
    oos = [build_trade(c, -1, 1.0, 1.0) for c in oos_c]
    oos_pairs = [(c, 1.0, 1.0) for c in oos_c]
    m_oos = with_gross(oos, metrics(oos))
    ci_lo, ci_hi = bootstrap_pf_ci(oos)

    # ── 基线 ──
    base = {"trend": [], "random_ind": [], "permutation": None, "inverse": None, "bh": None}

    # Trend baseline（双向，OOS 区间，与信号无关）
    trend_rows = []
    for c in cand:
        if not (oos_range[0] <= fmt_ts(c["t"]) < oos_range[1]):
            continue
        if c["regime"] == "TREND_UP":
            trend_rows.append(build_trade(c, +1, 1.0, 1.0))
        elif c["regime"] == "TREND_DOWN":
            trend_rows.append(build_trade(c, -1, 1.0, 1.0))
    base["trend"] = trend_rows

    rnd = random.Random(42)
    rand_pfs, rand_avgs = [], []
    for _ in range(N_MC):
        rows = [build_trade(c, rnd.choice((1, -1)), sm, tm) for c, sm, tm in oos_pairs]
        m = metrics(rows)
        if m["pf"] is not None:
            rand_pfs.append(m["pf"])
        rand_avgs.append(m["avg_ret"])
    rand_pfs.sort()
    _pf = m_oos["pf"]
    base["random_ind"] = {
        "n_mc": len(rand_pfs),
        "p_value": (round(sum(1 for p in rand_pfs if p >= (_pf or 0)) / len(rand_pfs), 4)
                    if rand_pfs and _pf else None),
        "pf_mean": round(statistics.fmean(rand_pfs), 3) if rand_pfs else None,
        "pf_p05": round(rand_pfs[int(0.05*len(rand_pfs))], 3) if rand_pfs else None,
        "pf_p95": round(rand_pfs[int(0.95*len(rand_pfs))], 3) if rand_pfs else None,
        "pf_min": round(min(rand_pfs), 3) if rand_pfs else None,
        "pf_max": round(max(rand_pfs), 3) if rand_pfs else None,
        "avg_mean": round(statistics.fmean(rand_avgs), 4) if rand_avgs else None,
        "pct_strategy_beats": round(sum(1 for p in rand_pfs if m_oos["pf"] and p < m_oos["pf"])
                                    / len(rand_pfs) * 100, 1) if rand_pfs and m_oos["pf"] else None,
    }

    # Mode B：Permutation（保多空比例）
    dirs = [r["dir"] for r in oos]
    if len(set(dirs)) > 1:
        perm_pfs = []
        for _ in range(N_MC):
            d = dirs[:]
            rnd.shuffle(d)
            rows = [build_trade(c, dd, sm, tm) for (c, sm, tm), dd in zip(oos_pairs, d)]
            m = metrics(rows)
            if m["pf"] is not None:
                perm_pfs.append(m["pf"])
        perm_pfs.sort()
        base["permutation"] = {
            "mode": "true_permutation",
            "p_value": (round(sum(1 for p in perm_pfs if p >= (_pf or 0)) / len(perm_pfs), 4)
                        if perm_pfs and _pf else None),
            "pf_mean": round(statistics.fmean(perm_pfs), 3) if perm_pfs else None,
            "pf_p05": round(perm_pfs[int(0.05*len(perm_pfs))], 3) if perm_pfs else None,
            "pf_p95": round(perm_pfs[int(0.95*len(perm_pfs))], 3) if perm_pfs else None,
            "pct_strategy_beats": round(sum(1 for p in perm_pfs if m_oos["pf"] and p < m_oos["pf"])
                                        / len(perm_pfs) * 100, 1) if perm_pfs else None,
        }
    else:
        # 方向单一 → 真置换退化，改用替代零假设 B′：随机入场时点（保留方向与笔数）
        pool = [c for c in cand]
        perm_pfs, perm_avgs = [], []
        k = len(oos)
        for _ in range(N_MC):
            picked = rnd.sample(pool, min(k, len(pool)))
            rows = [build_trade(c, -1, 1.0, 1.0) for c in picked]
            m = metrics(rows)
            if m["pf"] is not None:
                perm_pfs.append(m["pf"])
            perm_avgs.append(m["avg_ret"])
        perm_pfs.sort()
        base["permutation"] = {
            "mode": "timing_permutation_B_prime（方向单一，真置换退化为恒等）",
            "p_value": (round(sum(1 for p in perm_pfs if p >= (_pf or 0)) / len(perm_pfs), 4)
                        if perm_pfs and _pf else None),
            "pf_mean": round(statistics.fmean(perm_pfs), 3) if perm_pfs else None,
            "pf_p05": round(perm_pfs[int(0.05*len(perm_pfs))], 3) if perm_pfs else None,
            "pf_p95": round(perm_pfs[int(0.95*len(perm_pfs))], 3) if perm_pfs else None,
            "avg_mean": round(statistics.fmean(perm_avgs), 4) if perm_avgs else None,
            "pct_strategy_beats": round(sum(1 for p in perm_pfs if m_oos["pf"] and p < m_oos["pf"])
                                        / len(perm_pfs) * 100, 1) if perm_pfs else None,
        }

    # Buy & Hold / Short & Hold（OOS 区间）
    if windows:
        o0 = next(c["t"] for c in cand if fmt_ts(c["t"]) >= oos_range[0])
        o1 = next(c["t"] for c in reversed(cand) if fmt_ts(c["t"]) < oos_range[1])
        p0 = next(c["price"] for c in cand if c["t"] == o0)
        p1 = next(c["price"] for c in cand if c["t"] == o1)
        seg = [c["price"] for c in cand if o0 <= c["t"] <= o1]
        peak, mdd = seg[0], 0.0
        for x in seg:
            peak = max(peak, x)
            mdd = max(mdd, (peak - x) / peak * 100)
        base["bh"] = {"buy_hold_pct": round((p1 / p0 - 1) * 100, 2),
                      "short_hold_pct": round((p0 / p1 - 1) * 100, 2),
                      "max_dd_pct": round(mdd, 2),
                      "range": [fmt_ts(o0), fmt_ts(o1)]}

    # ── Cost Sensitivity（对 OOS 交易）──
    sens = {}
    for sc in PRESETS:
        idx_by_t = {c["t"]: c for c in cand}
        rows = []
        for r in oos:
            c = idx_by_t[r["t"]]
            rr = build_trade(c, r["dir"], scenario=sc,
                             sl_pct=r["sl_pct"], tp_pct=r["tp_pct"])
            rows.append(rr)
        m = with_gross(rows, metrics(rows))
        ci = bootstrap_pf_ci(rows)
        sens[sc] = {**m, "ci95": ci}

    # ── Regime 分桶（OOS，base 成本）──
    regime_buckets = {}
    for reg in sorted({r["regime"] for r in oos}):
        rows = [r for r in oos if r["regime"] == reg]
        if rows:
            regime_buckets[reg] = with_gross(rows, metrics(rows))

    # ── 月度分桶（跨时间稳定性）──
    monthly = {}
    for mk in sorted({datetime.fromtimestamp(r["t"] / 1000, TZ).strftime("%Y-%m") for r in oos}):
        rows = [r for r in oos if datetime.fromtimestamp(r["t"] / 1000, TZ).strftime("%Y-%m") == mk]
        if rows:
            monthly[mk] = metrics(rows)

    # ── 验收判定（§13 六条 + 概率）──
    trend_m = with_gross(trend_rows, metrics(trend_rows))
    sens_base, sens_cons = sens["base"], sens["conservative"]
    ci_lo_v = ci_lo if ci_lo is not None else -1
    p_perm_v = base["permutation"].get("p_value")
    p_rand_v = base["random_ind"].get("p_value")
    verdict = {
        "①跨时间稳定": all(mm["pf"] is not None and mm["pf"] > 1 for mm in monthly.values()),
        "②扣成本成立": (sens_base["pf"] or 0) > 1 and (sens_cons["pf"] or 0) > 1,
        "③WF稳定": (m_wf["pf"] or 0) > 1 and (wf_ci[0] or -1) > 1,
        "④优于纯趋势": (m_oos["pf"] or 0) > (trend_m["pf"] or 0),
        "⑤随机化通过": (p_perm_v is not None and p_perm_v < 0.05),
        "⑥随机方向通过": (p_rand_v is not None and p_rand_v < 0.05),
        "⑦NetPF>1且CI下界>1": (m_oos["pf"] or 0) > 1 and ci_lo_v > 1,
        "⑧概率已校准": False,
    }
    verdict["总体"] = all(verdict.values())

    return {
        "symbol": symbol, "days": days, "tier": tier,
        "decision_points": len(cand),
        "signal_points": sum(1 for c in cand if c["n_hit"] >= MIN_SIG),
        "windows": windows, "oos_metrics": {**m_oos, "ci95_net_pf": [ci_lo, ci_hi]},
        "oos_wf_metrics": {**m_wf, "ci95_net_pf": list(wf_ci)},
        "oos_default_metrics": {**m_oos, "ci95_net_pf": [ci_lo, ci_hi]},
        "oos_range": oos_range,
        "baselines": base, "cost_sensitivity": sens, "regime_buckets": regime_buckets,
        "monthly": monthly, "verdict": verdict, "trend_metrics": trend_m,
        "oos_trades": oos,
    }


def _fmt_m(m, label, extra=""):
    if not m or m.get("n", 0) == 0:
        return f"    {label}: 无样本"
    pf = f"{m['pf']:.2f}" if m.get("pf") is not None else "inf"
    gpf = f"{m['gross_pf']:.2f}" if m.get("gross_pf") is not None else "-"
    return (f"    {label}: n={m['n']:<4} 胜率{m['win_rate']:<6} GrossPF {gpf:>6} → NetPF {pf:>6} | "
            f"avg {m['avg_ret']:+.4f}% | 累计{m['total_ret_pct']:+.2f}% | 回撤{m['max_dd_pct']}% | "
            f"Sharpe(t){m['sharpe_per_trade']} {extra}")


def print_symbol_report(s, r):
    print("\n" + "=" * 80)
    print(f"  {s} | {r['days']}天 | 成本档 {r['tier']} | 决策点 {r['decision_points']} | "
          f"信号点 {r['signal_points']}")
    print(f"  OOS 区间: {r['oos_range'][0]} ~ {r['oos_range'][1]}")
    print("=" * 80)

    print("\n【一】当前策略 OOS（生产参数 1.0×ATR/1.0×ATR，因子≥3，不拟合）")
    print(_fmt_m(r["oos_default_metrics"], "OOS-Default", f"CI95 {r['oos_default_metrics']['ci95_net_pf']}"))

    print("\n【二】Walk-Forward OOS（训练30天选参 → 测试15天，滚动15天）")
    for i, w in enumerate(r["windows"], 1):
        tm = w["test_metrics"]
        pf = f"{tm['pf']:.2f}" if tm.get("pf") is not None else "-"
        print(f"    W{i} 测试 {w['test'][0][:10]}~{w['test'][1][:10]} | 选中参数 "
              f"SL{w['params']['sl']}×ATR TP{w['params']['tp']}×ATR 因子≥{w['params']['min_factors']} "
              f"| 测试 n={tm.get('n',0)} 胜率{tm.get('win_rate','-')} NetPF {pf}")
    print(_fmt_m(r["oos_wf_metrics"], "OOS-WF 汇总", f"CI95 {r['oos_wf_metrics']['ci95_net_pf']}"))
    print("    ⚠ 单窗口 PF 仅作描述性结果，不做有效性判断（样本不足）")

    b = r["baselines"]
    print("\n【三】基线对照（同 OOS 区间 / 同成本 / 同 SL-TP）")
    print(_fmt_m(with_gross(b["trend"], metrics(b["trend"])), "Trend Baseline "))
    ri = b["random_ind"]
    print(f"    Mode A 随机方向: PF均值 {ri['pf_mean']} [P5 {ri['pf_p05']} ~ P95 {ri['pf_p95']}] "
          f"min{ri['pf_min']} max{ri['pf_max']} | avg {ri['avg_mean']:+.4f}% "
          f"| p值 {ri['p_value']}（策略优于随机比例 {ri['pct_strategy_beats']}%）")
    pb = b["permutation"]
    print(f"    Mode B {pb['mode']}: PF均值 {pb['pf_mean']} [P5 {pb['pf_p05']} ~ P95 {pb['pf_p95']}] "
          f"| p值 {pb['p_value']}（策略优于置换比例 {pb['pct_strategy_beats']}%）")
    bh = b["bh"]
    if bh:
        print(f"    Buy&Hold {bh['buy_hold_pct']:+.2f}% | Short&Hold {bh['short_hold_pct']:+.2f}% "
              f"| 区间最大回撤 {bh['max_dd_pct']}%  ({bh['range'][0][:10]}~{bh['range'][1][:10]})")

    print("\n【四】成本敏感性（OOS，分档 + 生命周期 + Funding 事件）")
    print(f"    {'场景':<14}{'n':>4}{'GrossPF':>9}{'NetPF':>8}{'NetPF_CI95':>18}{'avg净%':>10}{'累计净%':>10}")
    for sc, m in r["cost_sensitivity"].items():
        pf = f"{m['pf']:.2f}" if m.get("pf") is not None else "inf"
        gpf = f"{m['gross_pf']:.2f}" if m.get("gross_pf") is not None else "-"
        print(f"    {sc:<14}{m['n']:>4}{gpf:>9}{pf:>8}{str(m['ci95']):>18}"
              f"{m['avg_ret']:>+10.4f}{m['total_ret_pct']:>+10.2f}")

    print("\n【五】Regime 分桶（OOS，base 成本）")
    for reg, m in r["regime_buckets"].items():
        print(_fmt_m(m, f"{reg:<12}"))

    print("\n【六】月度分桶（跨时间稳定性）")
    for mk, m in r["monthly"].items():
        pf = f"{m['pf']:.2f}" if m.get("pf") is not None else "-"
        print(f"    {mk}: n={m['n']:<4} 胜率{m['win_rate']:<6} NetPF {pf:>6} | avg {m['avg_ret']:+.4f}%")

    print("\n【七】验收判定（§13）")
    for k, ok in r["verdict"].items():
        if k == "总体":
            continue
        print(f"    {'✅' if ok else '❌'} {k}")
    print(f"    → 总体：{'✅ 通过' if r['verdict']['总体'] else '❌ 未通过（未证明存在 Alpha）'}")


def main():
    symbols = (sys.argv[1].split(",") if len(sys.argv) > 1
               else ["BTCUSDT", "ETHUSDT", "SOLUSDT", "SPCXUSDT"])
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    symbols = [s.upper() if s.upper().endswith("USDT") else s.upper() + "USDT" for s in symbols]

    out = {}
    for s in symbols:
        try:
            r = run_symbol(s, days)
            if r:
                out[s] = r
        except Exception as e:
            import traceback
            print(f"  ❌ {s} 失败: {e}")
            traceback.print_exc()

    with open(f"backtest_v31_report{SUFFIX}.json", "w", encoding="utf-8") as f:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "oos_trades"}
                   for k, v in out.items()}, f, ensure_ascii=False, indent=1)
    print(f"\n📁 明细已存: backtest_v31_report{SUFFIX}.json")

    for s, r in out.items():
        print_symbol_report(s, r)

    # ── 汇总表 ──
    print("\n" + "=" * 80)
    print("  V3.1 汇总（OOS-Default）")
    print("=" * 80)
    print(f"  {'币种':<9}{'n':>4}{'胜率':>7}{'GrossPF':>9}{'NetPF':>8}{'NetPF_CI95':>18}{'avg净%':>9}{'回撤%':>8}{'p置换':>8}{'判定':>8}")
    for s, r in out.items():
        m = r["oos_default_metrics"]
        ci = m["ci95_net_pf"]
        pf = f"{m['pf']:.2f}" if m["pf"] is not None else "inf"
        gpf = f"{m['gross_pf']:.2f}" if m.get("gross_pf") is not None else "-"
        pv = r["baselines"]["permutation"].get("p_value")
        print(f"  {s:<9}{m['n']:>4}{m['win_rate']:>7}{gpf:>9}{pf:>8}"
              f"{str(ci):>18}{m['avg_ret']:>+9.4f}{m['max_dd_pct']:>8}"
              f"{str(pv):>8}{'通过' if r['verdict']['总体'] else '未通过':>8}")


if __name__ == "__main__":
    main()
