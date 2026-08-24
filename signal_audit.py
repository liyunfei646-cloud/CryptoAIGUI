"""
signal_audit.py — 信号审计系统 (V2.0 Phase 1)
==============================================
对应《V2.0优化方案》第20节：每次产生信号都保存全字段快照，
事后记录 +5m/+15m/+30m/+60m/+4h 的 MFE/MAE/SL/TP/收益，
为 P2 概率校准与分桶胜率统计积累真实样本。

存储：signals/ 目录（JSONL，已加入 .gitignore）
  signals_pending.jsonl  待评估信号
  signals_done.jsonl     已评估信号

设计原则：
  - save_signal 幂等（同一 id 不重复写）
  - evaluate_pending 只评估已到期的窗口，未到期保持 pending
  - 所有评估只用【已收盘】K线，不引入未来函数
  - 任何异常都不影响主流程
"""

import json
import os
import sys
import time
from datetime import datetime

SIGNALS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals")
PENDING_FILE = os.path.join(SIGNALS_DIR, "signals_pending.jsonl")
DONE_FILE = os.path.join(SIGNALS_DIR, "signals_done.jsonl")

# 评估窗口: (名称, 秒)
WINDOWS = [
    ("5m", 5 * 60),
    ("15m", 15 * 60),
    ("30m", 30 * 60),
    ("60m", 60 * 60),
    ("4h", 4 * 3600),
]

# 超过该时长仍未完全评估的信号强制归档（避免 pending 无限膨胀）
MAX_PENDING_AGE_S = 72 * 3600


def _load_lines(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out


def _append(path: str, rec: dict):
    os.makedirs(SIGNALS_DIR, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def save_signal(snapshot: dict):
    """
    保存一条信号快照（幂等：同 id 不重复）。
    仅保存有明确入场建议的信号（direction 为 LONG/SHORT 且 entry/sl/tp 齐全）。
    """
    if not snapshot.get("id"):
        return
    if snapshot.get("direction") not in ("LONG", "SHORT"):
        return
    if not snapshot.get("entry") or not snapshot.get("stop_loss") or not snapshot.get("take_profit"):
        return

    rec = {
        "id": snapshot["id"],
        "time": snapshot.get("time", int(time.time() * 1000)),
        "symbol": snapshot.get("symbol", ""),
        "direction": snapshot.get("direction"),
        "price": snapshot.get("price"),
        "entry": snapshot.get("entry"),
        "stop_loss": snapshot.get("stop_loss"),
        "take_profit": snapshot.get("take_profit"),
        "sl_pct": snapshot.get("sl_pct"),
        "tp_pct": snapshot.get("tp_pct"),
        "regime": snapshot.get("regime"),
        "grade": snapshot.get("grade"),
        "long_score": snapshot.get("long_score"),
        "short_score": snapshot.get("short_score"),
        "rsi": snapshot.get("rsi"),
        "volume_ratio": snapshot.get("volume_ratio"),
        "oi_trend": snapshot.get("oi_trend"),
        "funding_regime": snapshot.get("funding_regime"),
        "taker_trend": snapshot.get("taker_trend"),
        "entry_trigger": snapshot.get("entry_trigger"),
        "outcomes": {},
        "evaluated_at": None,
    }

    # 幂等检查
    for line in _load_lines(PENDING_FILE):
        if line.get("id") == rec["id"]:
            return
    _append(PENDING_FILE, rec)


def _eval_signal(rec: dict, klines_5m: list[dict], now_ms: int) -> dict:
    """评估一条信号的所有到期窗口，返回 (rec, all_done)。"""
    t0 = rec["time"]
    entry = rec["entry"]
    direction = rec["direction"]
    sl_pct = rec.get("sl_pct") or 2.0
    tp_pct = rec.get("tp_pct") or 4.0

    outcomes = rec.get("outcomes") or {}
    all_done = True

    for name, secs in WINDOWS:
        if name in outcomes:
            continue
        t_end = t0 + secs * 1000
        if now_ms < t_end:
            all_done = False
            continue
        bars = [k for k in klines_5m if t0 <= k["time"] < t_end]
        if not bars:
            # 数据缺失（可能交易所无记录），标记 invalid 避免卡死
            outcomes[name] = {"status": "invalid"}
            continue
        high = max(b["high"] for b in bars)
        low = min(b["low"] for b in bars)
        last_close = bars[-1]["close"]

        if direction == "LONG":
            sl_hit = low <= entry * (1 - sl_pct / 100)
            tp_hit = high >= entry * (1 + tp_pct / 100)
            mfe = (high - entry) / entry * 100
            mae = (entry - low) / entry * 100
            ret = (last_close - entry) / entry * 100
        else:
            sl_hit = high >= entry * (1 + sl_pct / 100)
            tp_hit = low <= entry * (1 - tp_pct / 100)
            mfe = (entry - low) / entry * 100
            mae = (high - entry) / entry * 100
            ret = (entry - last_close) / entry * 100

        outcomes[name] = {
            "mfe": round(mfe, 3),
            "mae": round(mae, 3),
            "sl_hit": bool(sl_hit),
            "tp_hit": bool(tp_hit),
            "ret": round(ret, 3),
        }

    rec["outcomes"] = outcomes
    if all_done:
        rec["evaluated_at"] = int(now_ms)
    return rec, all_done


def evaluate_pending(now_ms: int = None):
    """
    评估所有到期窗口的信号。节流由调用方控制（analyzer 每次分析时调用）。
    返回本次归档（完成评估）的信号数。
    """
    try:
        from analyzer import fetch_klines  # 延迟导入避免循环依赖
    except Exception:
        return 0

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    pending = _load_lines(PENDING_FILE)
    if not pending:
        return 0

    # 按 symbol 分组拉 K 线（一次拉足够覆盖 4h 窗口 + 信号时间跨度）
    klines_cache: dict = {}
    remaining = []
    archived = 0

    for rec in pending:
        sym = rec.get("symbol", "")
        t0 = rec.get("time", 0)
        # 需要的 5m K线根数：从信号时刻到现在 + 4h 窗口
        need_ms = now_ms - t0 + 4 * 3600 * 1000 + 60 * 1000
        need_bars = max(50, int(need_ms / (5 * 60 * 1000)) + 10)
        need_bars = min(need_bars, 1500)  # 币安 5m 上限 1500

        if sym not in klines_cache:
            try:
                klines_cache[sym] = fetch_klines(sym, "5m", need_bars, closed_only=True)
            except Exception:
                klines_cache[sym] = []

        klines = klines_cache.get(sym) or []
        if not klines:
            remaining.append(rec)
            continue

        rec, done = _eval_signal(rec, klines, now_ms)
        if done or (now_ms - t0) > MAX_PENDING_AGE_S * 1000:
            _append(DONE_FILE, rec)
            archived += 1
        else:
            remaining.append(rec)

    # 写回 pending（原子替换）
    os.makedirs(SIGNALS_DIR, exist_ok=True)
    tmp = PENDING_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in remaining:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, PENDING_FILE)

    return archived


def stats() -> dict:
    """快速统计：已归档信号数与平均收益（供验证用）。"""
    done = _load_lines(DONE_FILE)
    if not done:
        return {"total": 0}
    wins = 0
    total_ret = 0.0
    for rec in done:
        r = rec.get("outcomes", {}).get("60m", {})
        ret = r.get("ret")
        if ret is not None:
            total_ret += ret
            if ret > 0:
                wins += 1
    n = sum(1 for rec in done if rec.get("outcomes", {}).get("60m", {}).get("ret") is not None)
    return {
        "total": len(done),
        "with_60m_outcome": n,
        "win_rate_60m": round(wins / n * 100, 1) if n else None,
        "avg_ret_60m": round(total_ret / n, 3) if n else None,
    }


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "evaluate":
        # 定时任务入口：评估所有已到期窗口（幂等，可每小时跑）
        try:
            n = evaluate_pending()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] evaluate: 归档 {n} 条, pending 剩余 {len(_load_lines(PENDING_FILE))}")
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] evaluate 异常: {e}")
    elif cmd == "stats":
        print("signals 目录:", SIGNALS_DIR)
        print("pending:", len(_load_lines(PENDING_FILE)))
        print("done:", len(_load_lines(DONE_FILE)))
        print("统计:", stats())
