"""
whale_score_engine.py — 巨鲸评分引擎 (Whale Score V2.0)
=======================================================
基于 WhaleScore_V1.md 设计文档 + 《V2.0优化方案》第5/6/7节重构。

V2.0 核心变更（数据源去重）：
  1. 订单簿只保留一个独立因子 orderbook_score (权重10%)
     —— 旧版 Exchange/Concentration/SmartMoney 三个因子都读订单簿，同一数据源重复计分
  2. Exchange 改用 Taker Buy/Sell（真实主动买卖资金流）
  3. Holding 改用 OI 变化 + Funding 趋势（OI 才是持仓量的直接度量）
  4. Smart Money 改用 多空账户比 + 强平结构 + Funding + 价格位置
  5. Concentration 降权至5%（CoinGecko Rank 不等于真实持仓集中度，仅作参考）

架构：
  ScoreBoard.fetch(symbol) → dict
    ├─ exchange_score     (权重 20%) — Taker 资金流
    ├─ holding_score       (权重 20%) — OI 变化 + Funding 趋势
    ├─ transfer_score      (权重 15%) — 大额转账活跃度（CoinGecko 代理）
    ├─ concentration_score (权重  5%) — CoinGecko Rank（参考）
    ├─ smart_money_score   (权重 30%) — 多空账户比 + 强平 + Funding
    ├─ orderbook_score     (权重 10%) — 订单簿失衡（唯一订单簿因子）
    └─ whale_score         (0~100)    — 加权聚合分

数据源优先级：
  1. 链上 API（Glassnode / Nansen / CoinGecko / CoinMetrics）
  2. 交易所数据（币安 OI/Taker/Funding/强平/订单簿）
  3. 保守回退值（50 分 = 中性）
"""

import json
import time
import ssl
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

# V2.0: 衍生品数据层（OI/Taker/Funding趋势/多空比/强平）
import futures_data

# ─── 全局配置 ────────────────────────────────────────────────────────────
COINGECKO_API = "https://api.coingecko.com/api/v3"
BINANCE_API = "https://api.binance.com"
BINANCE_FUTURES = "https://fapi.binance.com"
TZ = timezone(timedelta(hours=8))

# 评分因子权重（V2.1: transfer/concentration 伪因子停用，文档13节）
#  - transfer: 用 ticker count/成交额代理"大额转账"，无真实链上数据 → 退出决策
#  - concentration: 用 CoinGecko Rank 推断持仓集中度（市值排名≠地址集中度）→ 删除
#  - smart_money: 实际基于 OI/Funding/强平/多空比，改名 Positioning（文档14节）
# 权重重新归一化到剩余 4 因子（保留 orderbook 10% 上限，文档15节）
WEIGHTS = {
    "exchange": 0.25,
    "holding": 0.25,
    "transfer": 0.0,          # 停用
    "concentration": 0.0,     # 停用
    "smart_money": 0.40,      # 已改名为 Positioning
    "orderbook": 0.10,
}

# 停用的伪因子（仅保留显示，不参与加权）
DISABLED_FACTORS = ("transfer", "concentration")

_DATA_CACHE: dict = {}
_CACHE_TTL = 60  # 秒 — 超短线场景缓存较短


# ═══════════════════════════════════════════════════════════════════════
#  数据获取层
# ═══════════════════════════════════════════════════════════════════════

def _http_get(url: str, timeout: int = 10) -> bytes:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WhaleScore/1.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except Exception:
            if attempt < 1:
                time.sleep(0.3)
    return b""


def _cache_get(key: str) -> Optional[dict]:
    now = time.time()
    entry = _DATA_CACHE.get(key)
    if entry and (now - entry["ts"]) < _CACHE_TTL:
        return entry["data"]
    return None


def _cache_set(key: str, data: dict):
    _DATA_CACHE[key] = {"ts": time.time(), "data": data}


# ─── CoinGecko 辅助 ──────────────────────────────────────────────────

def _coingecko_id(symbol: str) -> Optional[str]:
    """将 USDT 交易对映射到 CoinGecko coin id。"""
    sym = symbol.upper().replace("USDT", "").lower()
    # 常见映射表
    COINGECKO_IDS = {
        "btc": "bitcoin",
        "eth": "ethereum",
        "sol": "solana",
        "bnb": "binancecoin",
        "xrp": "ripple",
        "ada": "cardano",
        "doge": "dogecoin",
        "dot": "polkadot",
        "avax": "avalanche-2",
        "matic": "matic-network",
        "link": "chainlink",
        "uni": "uniswap",
        "atom": "cosmos",
        "ltc": "litecoin",
        "bch": "bitcoin-cash",
        "trx": "tron",
        "etc": "ethereum-classic",
        "fil": "filecoin",
        "apt": "aptos",
        "sui": "sui",
        "op": "optimism",
        "arb": "arbitrum",
        "near": "near",
        "ftm": "fantom",
        "cro": "crypto-com-chain",
        "vet": "vechain",
        "theta": "theta-token",
        "icp": "internet-computer",
        "egld": "elrond-erd-2",
        "axs": "axie-infinity",
        "sand": "the-sandbox",
        "mana": "decentraland",
        "gala": "gala",
        "cake": "pancakeswap-token",
        "crv": "curve-dao-token",
        "aave": "aave",
        "comp": "compound",
        "mkr": "maker",
        "snx": "havven",
        "1inch": "1inch",
        "yfi": "yearn-finance",
        "people": "constitutiondao",
        "pepe": "pepe",
        "shib": "shiba-inu",
        "floki": "floki",
        "bonk": "bonk",
        "wif": "dogwifhat",
        "ordi": "ordinals",
        "sats": "sats",
        "rune": "thorchain",
        "fet": "fetch-ai",
        "agix": "singularitynet",
        "ocean": "ocean-protocol",
    }
    return COINGECKO_IDS.get(sym)


def _fetch_coin_gecko_data(symbol: str) -> dict:
    """从 CoinGecko 获取币种基本信息。"""
    coin_id = _coingecko_id(symbol)
    if not coin_id:
        return {}

    cached = _cache_get(f"cg_{coin_id}")
    if cached:
        return cached

    url = f"{COINGECKO_API}/coins/{coin_id}?localization=false&tickers=false&community_data=false&developer_data=false&sparkline=false"
    raw = _http_get(url)
    if not raw:
        return {}

    try:
        data = json.loads(raw.decode("utf-8"))
        result = {
            "market_cap_rank": data.get("market_cap_rank"),
            "market_cap": data.get("market_data", {}).get("market_cap", {}).get("usd"),
            "total_supply": data.get("market_data", {}).get("total_supply"),
            "circulating_supply": data.get("market_data", {}).get("circulating_supply"),
            "price_change_24h_pct": data.get("market_data", {}).get("price_change_percentage_24h"),
            "total_volume": data.get("market_data", {}).get("total_volume", {}).get("usd"),
        }
        _cache_set(f"cg_{coin_id}", result)
        return result
    except Exception:
        return {}


def _fetch_binance_orderbook(symbol: str, limit: int = 100) -> Optional[dict]:
    """获取币安订单簿，用于估算买卖压力。"""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    
    cached = _cache_get(f"ob_{sym}")
    if cached:
        return cached

    url = f"{BINANCE_API}/api/v3/depth?symbol={sym}&limit={limit}"
    raw = _http_get(url)
    if not raw:
        return None

    try:
        data = json.loads(raw.decode("utf-8"))
        bids = [[float(p), float(q)] for p, q in data.get("bids", [])]
        asks = [[float(p), float(q)] for p, q in data.get("asks", [])]
        bid_vol = sum(qty for _, qty in bids)
        ask_vol = sum(qty for _, qty in asks)
        bid_notional = sum(p * q for p, q in bids)
        ask_notional = sum(p * q for p, q in asks)
        result = {
            "bid_vol": bid_vol,
            "ask_vol": ask_vol,
            "bid_notional": bid_notional,
            "ask_notional": ask_notional,
            "imbalance_pct": ((bid_notional - ask_notional) / (bid_notional + ask_notional) * 100) if (bid_notional + ask_notional) > 0 else 0,
            "ratio": bid_notional / ask_notional if ask_notional > 0 else 1,
        }
        _cache_set(f"ob_{sym}", result)
        return result
    except Exception:
        return None


def _fetch_ticker_24h(symbol: str) -> Optional[dict]:
    """获取币安 24hr ticker 数据。"""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    
    cached = _cache_get(f"tk_{sym}")
    if cached:
        return cached

    for url in (
        f"{BINANCE_FUTURES}/fapi/v1/ticker/24hr?symbol={sym}",
        f"{BINANCE_API}/api/v3/ticker/24hr?symbol={sym}",
    ):
        raw = _http_get(url)
        if raw:
            try:
                data = json.loads(raw.decode("utf-8"))
                result = {
                    "price": float(data["lastPrice"]),
                    "change_pct": float(data["priceChangePercent"]),
                    "high": float(data["highPrice"]),
                    "low": float(data["lowPrice"]),
                    "volume": float(data["volume"]),
                    "quote_volume": float(data["quoteVolume"]),
                    "count": int(data.get("count", 0)),
                }
                _cache_set(f"tk_{sym}", result)
                return result
            except Exception:
                continue
    return None


def _fetch_funding_rate(symbol: str) -> Optional[dict]:
    """获取资金费率（用于持仓情绪判断）。"""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    
    cached = _cache_get(f"fr_{sym}")
    if cached:
        return cached

    url = f"{BINANCE_FUTURES}/fapi/v1/premiumIndex?symbol={sym}"
    raw = _http_get(url)
    if not raw:
        return None

    try:
        data = json.loads(raw.decode("utf-8"))
        fr = float(data.get("lastFundingRate", 0))
        result = {"funding_rate": fr, "signal": "neutral"}
        if fr > 0.0005:
            result["signal"] = "long_crowded"
        elif fr < -0.0005:
            result["signal"] = "short_crowded"
        _cache_set(f"fr_{sym}", result)
        return result
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
#  评分因子实现
# ═══════════════════════════════════════════════════════════════════════

def _score_exchange(data: dict) -> dict:
    """
    4.1 交易所资金流评分 (Exchange Score) — V2.0
    权重 20%

    数据源: Taker Buy/Sell（真实主动买卖资金流）
    V2.0 变更: 不再使用订单簿——订单簿已收敛为独立 orderbook 因子，
    避免同一数据源重复计分（文档第7节）。

    评分映射（Taker 买/卖比）：
      > 1.5   → 买盘强 → 80~95
      1.2~1.5 → 偏买   → 65~80
      0.95~1.2 → 中性  → 45~62
      < 0.8   → 偏卖   → 20~40
    """
    symbol = data.get("symbol", "")
    try:
        tk = futures_data.fetch_taker_series(symbol, "15m", 4)
    except Exception:
        tk = None
    if not tk or len(tk) < 2:
        return {"score": 50, "confidence": "low", "source": "default",
                "detail": "无法获取Taker数据，默认中性"}

    ratio = tk[-1]["ratio"]
    ratio_ago = tk[0]["ratio"]  # 45分钟前

    if ratio >= 1.5:
        score = 90
        detail = f"Taker主动买盘占优 (买/卖 {ratio:.2f})"
    elif ratio >= 1.2:
        score = 75
        detail = f"Taker买盘偏强 (买/卖 {ratio:.2f})"
    elif ratio >= 1.05:
        score = 62
        detail = f"Taker买盘略优 (买/卖 {ratio:.2f})"
    elif ratio >= 0.95:
        score = 50
        detail = f"Taker买卖均衡 (买/卖 {ratio:.2f})"
    elif ratio >= 0.8:
        score = 38
        detail = f"Taker卖盘略优 (买/卖 {ratio:.2f})"
    elif ratio >= 0.65:
        score = 25
        detail = f"Taker卖盘偏强 (买/卖 {ratio:.2f})"
    else:
        score = 10
        detail = f"Taker主动卖盘占优 (买/卖 {ratio:.2f})"

    # 趋势修正：当前 vs 45分钟前
    if ratio > ratio_ago * 1.2:
        score += 8
        detail += " | 买盘在增强"
    elif ratio < ratio_ago * 0.8:
        score -= 8
        detail += " | 卖压在增强"

    score = max(5, min(95, score))
    return {"score": score, "confidence": "medium", "source": "binance_taker",
            "detail": detail}


def _score_holding(data: dict) -> dict:
    """
    4.2 巨鲸持仓变化评分 (Holding Score) — V2.0
    权重 20%

    数据源: OI 变化（真实合约持仓量）+ Funding 趋势
    V2.0 变更: 用 OI 替代旧版"订单簿+单点Funding"代理——
    OI 才是持仓量的直接度量（文档第3节）。

    代理逻辑：
      - OI 15m 大幅增加 → 大资金建仓（方向结合价格）
      - OI 15m 大幅减少 → 大资金离场
      - Funding 绝对值大 → 杠杆拥挤风险
    """
    symbol = data.get("symbol", "")
    ticker = _fetch_ticker_24h(symbol)
    try:
        oi_hist = futures_data.fetch_oi_series(symbol, "15m", 5)
        frs = futures_data.fetch_funding_series(symbol, 6)
    except Exception:
        oi_hist = None
        frs = None

    if ticker is None:
        return {"score": 50, "confidence": "low", "source": "default", "detail": "无行情数据，默认中性"}

    change_pct = ticker["change_pct"]
    score = 55
    detail_parts = []

    # OI 变化（15m）→ 大资金建仓/离场
    if oi_hist and len(oi_hist) >= 2:
        oi_now = oi_hist[-1]["oi_value"]
        oi_prev = oi_hist[0]["oi_value"]
        if oi_prev > 0:
            oi_chg = (oi_now - oi_prev) / oi_prev * 100
            if oi_chg > 2:
                score += 12
                detail_parts.append(f"OI 15m +{oi_chg:.1f}% 新仓进场")
            elif oi_chg > 0.5:
                score += 5
                detail_parts.append(f"OI 15m +{oi_chg:.1f}% 缓慢增仓")
            elif oi_chg < -2:
                score -= 10
                detail_parts.append(f"OI 15m {oi_chg:.1f}% 大资金离场")
            elif oi_chg < -0.5:
                score -= 4
                detail_parts.append(f"OI 15m {oi_chg:.1f}% 减仓")
            # OI 方向与价格结合
            if oi_chg > 1 and change_pct > 2:
                score += 8
                detail_parts.append("增仓+上涨 (多头建仓)")
            elif oi_chg > 1 and change_pct < -2:
                score -= 8
                detail_parts.append("增仓+下跌 (空头建仓)")

    # Funding 趋势 → 杠杆拥挤度
    if frs and len(frs) >= 3:
        rates = [r["rate"] for r in frs]
        fr_now = rates[-1]
        if abs(fr_now) > 0.0005:
            score -= 8
            detail_parts.append(f"Funding {fr_now:+.5f} 杠杆过热")
        elif abs(fr_now) > 0.0001:
            score -= 3
            detail_parts.append(f"Funding {fr_now:+.5f} 略拥挤")

    score = max(10, min(95, score))
    detail = " | ".join(detail_parts) if detail_parts else "持仓变化平淡"
    conf = "medium" if oi_hist else "low"
    return {"score": score, "confidence": conf, "source": "futures_oi+funding",
            "detail": detail}


def _score_transfer(data: dict) -> dict:
    """
    4.3 大额转账活跃度评分 (Transfer Score)
    权重 15%

    使用撮合频次（ticker count）和成交波动作为大额转账活跃度代理。
    真实链上大额转账需要 Etherscan/BSCScan API 或 Nansen。

    代理逻辑：
      - ticker count = 24h内撮合次数，越高越活跃
      - 价格波动率作为额外参考
    """
    symbol = data.get("symbol", "")
    ticker = _fetch_ticker_24h(symbol)

    if ticker is None:
        return {"score": 50, "confidence": "low", "source": "default", "detail": "无行情数据，默认中性"}

    count = ticker.get("count", 0)
    quote_vol = ticker.get("quote_volume", 0)
    change_pct = abs(ticker["change_pct"])
    price = ticker["price"]

    # 山寨币交易频次特征
    # BTC 约 1M+ count/天，小币种差别很大
    if quote_vol > 0 and count > 0:
        avg_trade_size = quote_vol / count  # 平均每笔撮合额
    else:
        avg_trade_size = 0

    # 活跃度计算
    # 对山寨币来说，>5000次/天的撮合算活跃
    score = 50

    if count > 50000:
        score += 25
        detail = f"极高活跃度 ({count:,} 笔/天)"
    elif count > 20000:
        score += 18
        detail = f"高活跃度 ({count:,} 笔/天)"
    elif count > 10000:
        score += 10
        detail = f"中高活跃度 ({count:,} 笔/天)"
    elif count > 5000:
        score += 5
        detail = f"中等活跃度 ({count:,} 笔/天)"
    elif count > 1000:
        score -= 5
        detail = f"低活跃度 ({count:,} 笔/天)"
    else:
        score -= 15
        detail = f"极低活跃度 ({count:,} 笔/天)"

    # 平均单笔成交额大 → 可能有大资金活动
    if avg_trade_size > 5000:
        score += 10
        detail += f" | 大单密集 (${avg_trade_size:.0f}/笔)"
    elif avg_trade_size > 1000:
        score += 5
        detail += f" | 中大单 (${avg_trade_size:.0f}/笔)"

    # 价格波动大 → 可能有大资金在推动
    if change_pct > 15:
        score += 10
        detail += f" | 高波动 ({change_pct:.1f}%)"
    elif change_pct > 8:
        score += 5
        detail += f" | 中高波动 ({change_pct:.1f}%)"

    score = max(20, min(95, score))
    return {"score": score, "confidence": "medium", "source": "binance_ticker",
            "detail": detail}


def _score_concentration(data: dict) -> dict:
    """
    4.4 持仓集中度评分 (Concentration Score) — V2.0
    权重 5%（降权：文档第5节指出 CoinGecko Rank 不等于真实持仓集中度，
    本因子仅作参考，不再使用订单簿）

    评分：集中度越低分越高（分散 = 健康 = 高评分）
    """
    symbol = data.get("symbol", "")
    cg = _fetch_coin_gecko_data(symbol)
    cg_rank = cg.get("market_cap_rank")

    if not cg_rank:
        return {"score": 40, "confidence": "low", "source": "coingecko",
                "detail": "未上CoinGecko排名, 集中度风险"}

    if cg_rank <= 10:
        score = 85
        detail = f"主流币 (Rank #{cg_rank}), 分散度高"
    elif cg_rank <= 50:
        score = 70
        detail = f"大盘币 (Rank #{cg_rank})"
    elif cg_rank <= 200:
        score = 55
        detail = f"中盘币 (Rank #{cg_rank})"
    elif cg_rank <= 500:
        score = 40
        detail = f"小盘币 (Rank #{cg_rank}), 集中度风险"
    else:
        score = 25
        detail = f"微盘币 (Rank #{cg_rank}), 操纵风险大"

    return {"score": score, "confidence": "medium", "source": "coingecko",
            "detail": detail}


def _score_smart_money(data: dict) -> dict:
    """
    4.5 聪明钱评分 (Smart Money Score) — V2.0
    权重 30%

    数据源: 全账户多空比 + 最近强平方向 + Funding + 价格位置
    V2.0 变更: 不再使用订单簿（文档第7节去重）。

    代理逻辑（反向指标为主）：
      - 散户多空账户比极端 → 聪明钱反向
      - 多头/空头爆仓集中释放 → 流动性耗尽，反转信号
      - Funding 极端拥挤 → 反向
    """
    symbol = data.get("symbol", "")
    ticker = _fetch_ticker_24h(symbol)
    try:
        ls = futures_data.fetch_global_ls_ratio(symbol, "1h")
        liq = futures_data.fetch_recent_liquidations(symbol)
        frs = futures_data.fetch_funding_series(symbol, 4)
    except Exception:
        ls = None
        liq = None
        frs = None

    if ticker is None:
        return {"score": 50, "confidence": "low", "source": "default", "detail": "数据不足，默认中性"}

    price = ticker["price"]
    change_pct = ticker["change_pct"]
    high_24h = ticker["high"]
    low_24h = ticker["low"]
    score = 50
    signals = []

    range_24h = high_24h - low_24h
    range_pos = (price - low_24h) / range_24h if range_24h > 0 else 0.5

    # 1) 散户多空账户比（反向指标）
    if ls is not None:
        if ls > 2.5:
            score -= 12
            signals.append(f"散户多头拥挤 (LS {ls:.2f}), 聪明钱可能反向做空")
        elif ls < 0.4:
            score += 12
            signals.append(f"散户空头拥挤 (LS {ls:.2f}), 聪明钱可能反向做多")

    # 2) 强平结构（流动性释放）
    if liq and liq.get("count", 0) > 0:
        liq_long = liq.get("long") or 0
        liq_short = liq.get("short") or 0
        if liq_long > 0 and liq_long > liq_short * 2 and change_pct < -1:
            score += 10
            signals.append("多头爆仓集中释放, 抛压衰竭")
        elif liq_short > 0 and liq_short > liq_long * 2 and change_pct > 1:
            score += 10
            signals.append("空头爆仓集中释放, 逼空动能强")

    # 3) Funding 拥挤度（反向）
    if frs and len(frs) >= 2:
        fr_now = frs[-1]["rate"]
        if fr_now > 0.0005:
            score -= 8
            signals.append(f"多头杠杆过热 (funding {fr_now:+.5f})")
        elif fr_now < -0.0005:
            score += 8
            signals.append(f"空头杠杆过热 (funding {fr_now:+.5f})")

    # 4) 价格位置 + 涨跌
    if range_pos > 0.8 and change_pct > 10:
        score -= 8
        signals.append(f"高位大涨 ({change_pct:+.1f}%), 追高需谨慎")
    elif range_pos < 0.2 and change_pct < -10:
        score += 5
        signals.append(f"低位大跌 ({change_pct:+.1f}%), 关注衰竭信号")

    score = max(10, min(95, score))
    detail = "; ".join(signals) if signals else "无明显聪明钱信号"
    conf = "medium" if signals else "low"
    return {"score": score, "confidence": conf, "source": "futures_ls+liq+funding",
            "detail": detail}


def _score_orderbook(data: dict) -> dict:
    """
    4.6 订单簿失衡评分 (Orderbook Score) — V2.0 新增
    权重 10%（文档第7节：订单簿只保留一个独立因子，最大权重 ≤10%）

    数据源: 币安订单簿（全引擎唯一使用订单簿的因子）
    """
    ob = _fetch_binance_orderbook(data.get("symbol", ""))
    if ob is None:
        return {"score": 50, "confidence": "low", "source": "default",
                "detail": "无法获取订单簿数据，默认中性"}

    ratio = ob["ratio"]
    imbalance = ob["imbalance_pct"]

    if imbalance > 30:
        score = 85
        detail = f"买方大幅占优 (买卖比 {ratio:.2f})"
    elif imbalance > 15:
        score = 72
        detail = f"买方明显占优 (买卖比 {ratio:.2f})"
    elif imbalance > 5:
        score = 60
        detail = f"买方略占优 (买卖比 {ratio:.2f})"
    elif imbalance > -5:
        score = 50
        detail = f"买卖均衡 (买卖比 {ratio:.2f})"
    elif imbalance > -15:
        score = 40
        detail = f"卖方略占优 (买卖比 {ratio:.2f})"
    elif imbalance > -30:
        score = 28
        detail = f"卖方明显占优 (买卖比 {ratio:.2f})"
    else:
        score = 15
        detail = f"卖方大幅占优 (买卖比 {ratio:.2f})"

    return {"score": score, "confidence": "medium", "source": "binance_orderbook",
            "detail": detail}


# ═══════════════════════════════════════════════════════════════════════
#  聚合评分
# ═══════════════════════════════════════════════════════════════════════

def calc_whale_score(symbol: str) -> dict:
    """
    计算 Whale Score 综合评分。

    V2.1 变更：
      - transfer / concentration 伪因子停用（不参与加权，仅保留 detail 供显示）
      - smart_money 因子改名为 positioning（实际数据为 OI/Funding/强平/多空比）
      - 本评分仅供信息展示；不再参与交易决策（analyzer 中已移除）

    返回:
      whale_score    : 0~100 综合分（仅活跃因子加权）
      grade_label    : 极度看空 / 偏空 / 中性 / 偏多 / 极度看多
      factors        : 各因子详情
      confidence     : overall 置信度
    """
    data = {"symbol": symbol}

    exchange = _score_exchange(data)
    holding = _score_holding(data)
    transfer = _score_transfer(data)
    concentration = _score_concentration(data)
    smart_money = _score_smart_money(data)
    orderbook = _score_orderbook(data)

    factors = {
        "exchange": exchange,
        "holding": holding,
        "transfer": transfer,
        "concentration": concentration,
        "smart_money": smart_money,
        "orderbook": orderbook,
    }
    # V2.1: smart_money → positioning 别名
    factors["positioning"] = smart_money

    # 加权聚合（仅活跃因子；停用因子权重为 0）
    ws = 0.0
    w_sum = 0.0
    active = []
    for fname, w in WEIGHTS.items():
        if w <= 0 or fname not in factors:
            continue
        ws += factors[fname]["score"] * w
        w_sum += w
        active.append(fname)
    if w_sum > 0:
        ws /= w_sum

    # 置信度：活跃因子置信度的加权平均
    conf_map = {"high": 1.0, "medium": 0.7, "low": 0.4}
    overall_conf = 0.0
    for fname in active:
        overall_conf += conf_map.get(factors[fname].get("confidence", "low"), 0.5)
    overall_conf /= len(active) if active else 1.0

    # 等级标签
    if ws >= 80:
        label = "极度看多 🐋"
    elif ws >= 60:
        label = "偏多 📈"
    elif ws >= 40:
        label = "中性 ➖"
    elif ws >= 20:
        label = "偏空 📉"
    else:
        label = "极度看空 🐻"

    return {
        "whale_score": round(ws, 1),
        "grade_label": label,
        "confidence": round(overall_conf, 2),
        "factors": factors,
        "disabled_factors": list(DISABLED_FACTORS),
    }


# ═══════════════════════════════════════════════════════════════════════
#  快速测试
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    
    def test(sym: str):
        print(f"\n{'='*60}")
        print(f"  Whale Score — {sym}")
        print(f"{'='*60}")
        result = calc_whale_score(sym)
        print(f"  综合分: {result['whale_score']}  |  {result['grade_label']}")
        print(f"  置信度: {result['confidence']:.0%}")
        print(f"  {'─'*40}")
        for name, factor in result["factors"].items():
            print(f"  {name:>20} : {factor['score']:3d}  [{factor['confidence']:>6}]  {factor['detail'][:50]}")
        print(f"  {'─'*40}")
        return result

    if len(sys.argv) > 1:
        test(sys.argv[1])
    else:
        # 默认测试
        test("BTC")
        test("ETH")
        test("SOL")
