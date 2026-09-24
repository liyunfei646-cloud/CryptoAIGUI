#!/usr/bin/env python3
"""
build_gate.py — 从 V3.1 回测报告生成实盘闸门数据 v31_gate.json
================================================================
供 coin_analysis_4h.py 读取，实现 §11/§12 的 Trade Permission 判定。
数据源：backtest_v31_report_sig3.json（4币 × 90天 OOS，MIN_SIG=3）

闸门规则（9/20 立法 + V3.1 §13）：
  Net PF > 1 且 95%CI 下界 > 1 且 置换 p<0.05 且 优于纯趋势基线 → TRADE
  否则 → NO TRADE
用法: python3 build_gate.py [report.json]
"""
import json
import sys
from datetime import datetime


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "backtest_v31_report_sig3.json"
    with open(src, encoding="utf-8") as f:
        rep = json.load(f)

    gate = {}
    for sym, r in rep.items():
        m = r.get("oos_default_metrics", {})
        tre = r.get("trend_metrics", {})
        bl = r.get("baselines", {})
        gross_avg = m.get("gross_avg_ret")
        net_avg = m.get("avg_ret")
        cost = round(gross_avg - net_avg, 4) if (gross_avg is not None and net_avg is not None) else None
        perm_p = bl.get("permutation", {}).get("p_value")
        rand_p = bl.get("random_ind", {}).get("p_value")
        net_pf = m.get("pf")
        ci = m.get("ci95_net_pf") or [None, None]
        trend_pf = tre.get("pf")

        ok = (net_pf is not None and net_pf > 1
              and ci[0] is not None and ci[0] > 1
              and perm_p is not None and perm_p < 0.05
              and (trend_pf is None or net_pf > trend_pf))

        gate[sym] = {
            "source": f"{src} | 90天 OOS | 基准成本",
            "n": m.get("n"),
            "win_rate": m.get("win_rate"),
            "gross_pf": m.get("gross_pf"),
            "net_pf": net_pf,
            "net_pf_ci": ci,
            "ev_per_trade": net_avg,
            "cost_pct": cost,
            "trend_pf": trend_pf,
            "p_perm": perm_p,
            "p_rand": rand_p,
            "verdict": bool(ok and r.get("verdict", {}).get("总体")),
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    with open("v31_gate.json", "w", encoding="utf-8") as f:
        json.dump(gate, f, ensure_ascii=False, indent=1)

    print(f"📁 v31_gate.json 已生成（{len(gate)} 币，来源 {src}）")
    for s, g in gate.items():
        print(f"  {s:<10} NetPF {g['net_pf']} CI{g['net_pf_ci']} p置换 {g['p_perm']} "
              f"→ {'TRADE' if g['verdict'] else 'NO TRADE'}")


if __name__ == "__main__":
    main()
