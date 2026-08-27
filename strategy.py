"""
strategy.py — V2.1 Strategy 层
==============================
对应《V2.0代码整改与V2.1策略研究实施说明》第 9/10/11/12/21/22 节。

策略是交易假设的载体；Market Regime 是市场状态分类。
原则：
  - Regime 不锁死方向，由 Strategy 决定"当前假设是否值得研究/交易"
  - 逆势策略（趋势中做反转）允许研究，但提高确认门槛
  - 每个 READY 信号必须能解释"为什么现在交易"（reason_codes）
  - 未检测到任何策略 = NO_SETUP = 不交易

四大策略：
  TREND_CONTINUATION  趋势延续：顺势回撤到供需区（Fib/结构位）
  LIQUIDITY_REVERSAL  流动性反转：扫前低/前高后快速收回（假突破）
  RANGE_REVERSION     区间回归：贴近区间边界
  BREAKOUT            区间突破：向突破方向顺势
"""

STRATEGY_NONE = "NONE"
STRATEGY_TREND = "TREND_CONTINUATION"
STRATEGY_REVERSAL = "LIQUIDITY_REVERSAL"
STRATEGY_RANGE = "RANGE_REVERSION"
STRATEGY_BREAKOUT = "BREAKOUT"

ALL_STRATEGIES = (STRATEGY_TREND, STRATEGY_REVERSAL, STRATEGY_RANGE, STRATEGY_BREAKOUT)

# ─── 交易域配置（V2.1，文档第32节：LONG 与 SHORT 必须独立验证）───
# 取值:
#   trend_up_long   只做 TREND_UP + 趋势延续 + LONG（默认。20天回测：方向过滤后整体由负转正，
#                   PF 0.90→2.21；主流三币 PF 3.03。注意：样本内时间分布集中，尚未达"宣布有效"标准，
#                   属研究阶段配置，靠 signal_audit 积累真实样本）
#   all             全部候选方向（信息面板展示用，不产生交易建议）
# 可通过环境变量 TRADE_DOMAIN 覆盖（回测/实盘共用同一信号定义，文档43节）
import os as _os
TRADE_DOMAIN = _os.environ.get("TRADE_DOMAIN", "trend_up_long").lower()


def domain_allows(regime_name: str, strategy: str, direction: str) -> tuple:
    """
    交易域过滤：候选方向是否被允许交易。
    返回 (allowed, reason)。域外候选 → NO_TRADE（不交易，不产生 READY）。
    """
    if TRADE_DOMAIN in ("all", ""):
        return True, ""
    if TRADE_DOMAIN == "trend_up_long":
        if strategy == STRATEGY_TREND and regime_name == "TREND_UP" and direction == "LONG":
            return True, ""
        return False, f"交易域[trend_up_long]: 仅允许 TREND_UP+趋势延续+LONG"
    if TRADE_DOMAIN == "long_only":
        if direction == "LONG":
            return True, ""
        return False, "交易域[long_only]: 禁止做空"
    return True, ""

# 逆势策略需要额外满足的确认条件数（文档第19节：提高门槛）
COUNTER_TREND_EXTRA_CONFIRM = 1

# 位置阈值：策略认为"进入供需区"的最大距离（%）
ENTRY_ZONE_PCT = 2.0


def _regime_allows(regime_name: str, strategy: str, direction: str) -> tuple:
    """
    策略 × Regime 允许矩阵。
    返回 (allowed, is_counter_trend, reason)。
    注意：allowed 只代表"该假设值得研究"，不等于 READY。
    """
    r = regime_name
    ct = False  # counter-trend 标志

    if r == "BREAKOUT":
        if strategy == STRATEGY_BREAKOUT and direction == "LONG":
            return True, ct, "向上突破20根4H区间，顺势做多"
        return False, ct, f"{r} 仅允许突破方向"
    if r == "BREAKDOWN":
        if strategy == STRATEGY_BREAKOUT and direction == "SHORT":
            return True, ct, "向下跌破20根4H区间，顺势做空"
        return False, ct, f"{r} 仅允许突破方向"
    if r == "TREND_UP":
        if strategy == STRATEGY_TREND and direction == "LONG":
            return True, ct, "4H多头趋势，回撤顺势做多"
        if strategy == STRATEGY_REVERSAL and direction == "SHORT":
            return True, True, "上升趋势中做流动性反转（逆势，提高门槛）"
        return False, ct, f"{r} 不允许 {strategy}/{direction}"
    if r == "TREND_DOWN":
        if strategy == STRATEGY_TREND and direction == "SHORT":
            return True, ct, "4H空头趋势，反弹顺势做空"
        if strategy == STRATEGY_REVERSAL and direction == "LONG":
            return True, True, "下降趋势中做流动性反转（逆势，提高门槛）"
        return False, ct, f"{r} 不允许 {strategy}/{direction}"
    if r == "RANGE":
        if strategy == STRATEGY_RANGE:
            return True, ct, "震荡区间，边界回归"
        if strategy == STRATEGY_REVERSAL:
            return True, True, "震荡区间流动性反转（提高门槛）"
        return False, ct, f"{r} 不允许 {strategy}"
    if r == "CHAOS":
        if strategy == STRATEGY_REVERSAL:
            return True, True, "混沌环境反转（高门槛）"
        return False, ct, f"{r} 不允许 {strategy}"
    return False, ct, f"未知 regime {r}"


def detect_strategy(regime_name: str, ctx: dict) -> dict:
    """
    检测当前最可能的策略与候选方向。

    ctx 需提供：
      struct_trend        : 结构趋势 bullish/bearish/neutral
      patterns            : detect_candlestick_patterns 输出
      sup_dist_pct        : 到最近支撑距离(%) 或 None
      res_dist_pct        : 到最近阻力距离(%) 或 None
      fib_nearest         : 最近Fib名称 或 None
      fib_touch           : 是否触及Fib
      price, ema20        : 当前价 / EMA20(15m)
      high_volatility     : 是否高波动
      range_pct           : 24h区间百分位(0~100)

    返回:
      strategy / setup / candidate_direction / counter_trend
      allow_long / allow_short / reason_codes / block_reason
    """
    out = {
        "strategy": STRATEGY_NONE,
        "setup": "NONE",
        "candidate_direction": "NEUTRAL",
        "allow_long": False,
        "allow_short": False,
        "counter_trend": False,
        "reason_codes": [],
        "block_reason": "",
    }

    patterns = ctx.get("patterns") or {}
    sup_d = ctx.get("sup_dist_pct")
    res_d = ctx.get("res_dist_pct")
    fib_near = ctx.get("fib_nearest")
    fib_touch = ctx.get("fib_touch")
    price = ctx.get("price")
    ema20 = ctx.get("ema20")
    range_pct = ctx.get("range_pct")

    # 位置判断（策略级宽松阈值，Entry Trigger 再严格复查）
    sup_near = sup_d is not None and sup_d <= ENTRY_ZONE_PCT
    res_near = res_d is not None and res_d <= ENTRY_ZONE_PCT
    fib_retrace_long = fib_near and fib_touch and price is not None and ema20 is not None and price <= ema20
    fib_retrace_short = fib_near and fib_touch and price is not None and ema20 is not None and price >= ema20

    fake_break_below = bool(patterns.get("fake_break_below"))
    fake_break_above = bool(patterns.get("fake_break_above"))

    # ── 候选列表（按优先级尝试，允许矩阵拦截后 fallthrough）──
    candidates = []
    # 形态策略（流动性反转）优先——扫盘收回是最明确的入场形态
    if fake_break_below:
        candidates.append((STRATEGY_REVERSAL, "LONG", "SWEEP_LOW_RECLAIM"))
    if fake_break_above:
        candidates.append((STRATEGY_REVERSAL, "SHORT", "SWEEP_HIGH_REJECT"))
    # 状态策略
    if regime_name == "BREAKOUT":
        candidates.append((STRATEGY_BREAKOUT, "LONG", "RANGE_BREAKOUT"))
    elif regime_name == "BREAKDOWN":
        candidates.append((STRATEGY_BREAKOUT, "SHORT", "RANGE_BREAKDOWN"))
    if regime_name == "RANGE":
        if sup_near or (range_pct is not None and range_pct <= 15):
            candidates.append((STRATEGY_RANGE, "LONG", "RANGE_LOW"))
        if res_near or (range_pct is not None and range_pct >= 85):
            candidates.append((STRATEGY_RANGE, "SHORT", "RANGE_HIGH"))
    if regime_name == "TREND_UP" and (sup_near or fib_retrace_long):
        candidates.append((STRATEGY_TREND, "LONG", "PULLBACK_DEMAND"))
    if regime_name == "TREND_DOWN" and (res_near or fib_retrace_short):
        candidates.append((STRATEGY_TREND, "SHORT", "PULLBACK_SUPPLY"))

    for strategy, direction, setup in candidates:
        allowed, ct, reason = _regime_allows(regime_name, strategy, direction)
        if not allowed:
            out["block_reason"] = reason
            continue
        out.update({
            "strategy": strategy,
            "setup": setup,
            "candidate_direction": direction,
            "allow_long": direction == "LONG",
            "allow_short": direction == "SHORT",
            "counter_trend": ct,
            "reason_codes": [f"STRATEGY_{strategy}", f"SETUP_{setup}", f"REGIME_{regime_name}"],
            "block_reason": "",
        })
        return out

    # 无策略命中
    out["block_reason"] = out["block_reason"] or "无匹配策略/位置条件"
    return out


def required_confirm_count(counter_trend: bool, high_volatility: bool) -> int:
    """确认条件数量：顺势 2，逆势 +1，高波动再 +1（文档第8/19节）。"""
    n = 2
    if counter_trend:
        n += COUNTER_TREND_EXTRA_CONFIRM
    if high_volatility:
        n += 1
    return n
