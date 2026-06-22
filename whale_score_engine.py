"""
whale_score_engine.py — 巨鲸评分引擎 (Whale Score V1.0)
======================================================
根据 WhaleScore_V1.md 设计文档实现。

架构：
  ScoreBoard.fetch(symbol) → dict
    ├─ exchange_score     (权重 30%) — 交易所资金流
    ├─ holding_score       (权重 25%) — 巨鲸持仓变化
    ├─ transfer_score      (权重 15%) — 大额转账活跃度
    ├─ concentration_score (权重 10%) — 持仓集中度
    ├─ smart_money_score   (权重 20%) — 聪明钱行为
    └─ whale_score         (0~100)    — 加权聚合分

数据源优先级：
  1. 链上 API（Glassnode / Nansen / CoinGecko / CoinMetrics）
  2. 交易所数据估算（币安订单簿/成交数据）
  3. 保守回退值（50 分 = 中性）

超短线场景适配：
  - 对于小币种（山寨币），多数链上数据不可用
  - 使用交易所级代理指标估算巨鲸行为
  - 评分结果作为 FinalScore 中的 20% 权重因子
"""

import json
import time
import ssl
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

# ─── 全局配置 ────────────────────────────────────────────────────────────
COINGECKO_API = "https://api.coingecko.com/api/v3"
BINANCE_API = "https://api.binance.com"
BINANCE_FUTURES = "https://fapi.binance.com"
TZ = timezone(timedelta(hours=8))

# 评分因子权重（与文档一致）
WEIGHTS = {
    "exchange": 0.30,
    "holding": 0.25,
    "transfer": 0.15,
    "concentration": 0.10,
    "smart_money": 0.20,
}

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
    4.1 交易所资金流评分 (Exchange Score)
    权重 30%

    使用订单簿买卖压力作为交易所资金流的代理指标。
    真实交易所净流出需要 Glassnode/CoinMetrics 等链上数据源。

    评分映射（订单簿买卖比 → 净流出等价分）：
      买卖比 > 1.5  → 看多（买方强）→ 80~100
      买卖比 1.0~1.5 → 偏多 → 60~80
      买卖比 0.8~1.0 → 中性 → 40~60
      买卖比 < 0.8  → 偏空 → 20~40
    """
    ob = _fetch_binance_orderbook(data.get("symbol", ""))
    
    if ob is None:
        return {"score": 50, "confidence": "low", "source": "default", "detail": "无法获取订单簿数据，默认中性"}

    ratio = ob["ratio"]
    imbalance = ob["imbalance_pct"]

    # 根据买卖不平衡比例计算分数
    if imbalance > 30:
        score = 100
        detail = f"买方大幅占优 (买卖比 {ratio:.2f})"
    elif imbalance > 15:
        score = 85
        detail = f"买方明显占优 (买卖比 {ratio:.2f})"
    elif imbalance > 5:
        score = 70
        detail = f"买方略占优 (买卖比 {ratio:.2f})"
    elif imbalance > -5:
        score = 55
        detail = f"买卖均衡 (买卖比 {ratio:.2f})"
    elif imbalance > -15:
        score = 40
        detail = f"卖方略占优 (买卖比 {ratio:.2f})"
    elif imbalance > -30:
        score = 25
        detail = f"卖方明显占优 (买卖比 {ratio:.2f})"
    else:
        score = 15
        detail = f"卖方大幅占优 (买卖比 {ratio:.2f})"

    return {"score": score, "confidence": "medium", "source": "binance_orderbook",
            "detail": detail}


def _score_holding(data: dict) -> dict:
    """
    4.2 巨鲸持仓变化评分 (Holding Score)
    权重 25%

    使用资金费率的方向和资金动向作为持仓变化的代理指标。
    真实巨鲸地址数变化需要 Glassnode 等链上数据。

    代理逻辑：
      - 资金费率极度负值（空头拥挤）+ 市场上涨 → 巨鲸可能在建多仓
      - 资金费率极度正值（多头拥挤）+ 市场下跌 → 巨鲸可能在平多/建空
      - 活跃成交数变化作为交易活跃度参考
    """
    symbol = data.get("symbol", "")
    ticker = _fetch_ticker_24h(symbol)
    funding = _fetch_funding_rate(symbol)

    if ticker is None:
        return {"score": 50, "confidence": "low", "source": "default", "detail": "无行情数据，默认中性"}

    price = ticker["price"]
    change_pct = ticker["change_pct"]
    vol = ticker.get("volume", 0)
    quote_vol = ticker.get("quote_volume", 0)

    score = 60  # 默认中性偏高（山寨币通常有一定集中度）

    # 使用成交额变化作为持仓活跃度代理
    # 高成交额 + 价格方向 = 可能的大资金行为
    cg = _fetch_coin_gecko_data(symbol)
    cg_vol = cg.get("total_volume", 0) or quote_vol
    mc = cg.get("market_cap") or 0

    if mc > 0 and cg_vol > 0:
        vol_to_mc = cg_vol / mc
        # 成交额/市值比高 → 换手率高 → 活跃
        if vol_to_mc > 0.3:
            score += 15  # 高活跃度，大资金可能在活动
            detail = f"高换手率 ({vol_to_mc:.1%}), 巨鲸活跃"
        elif vol_to_mc > 0.15:
            score += 8
            detail = f"中等换手率 ({vol_to_mc:.1%}), 正常"
        elif vol_to_mc > 0.05:
            score -= 5
            detail = f"低换手率 ({vol_to_mc:.1%}), 大资金不活跃"
        else:
            score -= 15
            detail = f"极度低换手率 ({vol_to_mc:.1%}), 流动性不足"
    else:
        # 没有市值数据，用成交额绝对值估算
        if quote_vol > 100_000_000:
            score += 10
            detail = f"高成交额 (${quote_vol/1e6:.0f}M)"
        elif quote_vol > 10_000_000:
            score += 5
            detail = f"中等成交额 (${quote_vol/1e6:.0f}M)"
        elif quote_vol > 1_000_000:
            score += 0
            detail = f"偏低成交额 (${quote_vol/1e6:.0f}M)"
        else:
            score -= 15
            detail = f"极低成交额 (${quote_vol/1e6:.0f}M), 流动性风险"

    # 资金费率方向修正
    if funding:
        fr = funding["funding_rate"]
        if funding["signal"] == "short_crowded" and change_pct > 0:
            # 空头拥挤 + 价格上涨 → 轧空迹象 → 巨鲸可能在逼空
            score += 10
            detail += " | 空头拥挤+上涨，逼空可能"
        elif funding["signal"] == "long_crowded" and change_pct < 0:
            # 多头拥挤 + 价格下跌 → 多头踩踏 → 巨鲸可能在出货
            score -= 10
            detail += " | 多头拥挤+下跌，出货风险"

    score = max(10, min(95, score))
    conf = "medium" if cg.get("market_cap") else "low"
    return {"score": score, "confidence": conf, "source": "proxy",
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
    4.4 持仓集中度评分 (Concentration Score)
    权重 10%

    使用市场深度和成交额分布作为集中度代理。
    真实 Top10 持仓占比需要 CoinGecko 或链上 API。

    评分：集中度越低分越高（分散 = 健康 = 高评分）
    """
    symbol = data.get("symbol", "")
    ob = _fetch_binance_orderbook(symbol)
    ticker = _fetch_ticker_24h(symbol)
    cg = _fetch_coin_gecko_data(symbol)

    if ob is None or ticker is None:
        return {"score": 50, "confidence": "low", "source": "default", "detail": "数据不足，默认中性"}

    score = 60

    # 使用订单簿深度作为集中度代理
    bid_vol = ob["bid_vol"]
    ask_vol = ob["ask_vol"]
    total_depth = bid_vol + ask_vol
    imbalance = abs(ob["imbalance_pct"])

    # 极端不平衡 → 集中度高（可能被操纵）
    if imbalance > 50:
        score -= 20
        detail = f"订单簿极度不平衡 ({imbalance:.0f}%), 操纵风险高"
    elif imbalance > 30:
        score -= 10
        detail = f"订单簿明显不平衡 ({imbalance:.0f}%), 集中度偏高"
    elif imbalance > 15:
        score -= 5
        detail = f"订单簿轻度不平衡 ({imbalance:.0f}%)"
    else:
        score += 10
        detail = f"订单簿均衡分散 ({imbalance:.0f}%)"

    # 成交额分散度（CoinGecko rank 作为参考）
    cg_rank = cg.get("market_cap_rank")
    if cg_rank:
        if cg_rank <= 10:
            score += 20
            detail += f" | 主流币 (Rank #{cg_rank}), 分散度高"
        elif cg_rank <= 50:
            score += 10
            detail += f" | 大盘币 (Rank #{cg_rank})"
        elif cg_rank <= 200:
            score += 0
            detail += f" | 中盘币 (Rank #{cg_rank})"
        elif cg_rank <= 500:
            score -= 10
            detail += f" | 小盘币 (Rank #{cg_rank}), 集中度风险"
        else:
            score -= 20
            detail += f" | 微盘币 (Rank #{cg_rank}), 集中度高, 操纵风险大"
    else:
        # 没有排名 → 微盘币
        score -= 15
        detail += " | 未上CoinGecko排名, 高度集中风险"

    score = max(10, min(95, score))
    return {"score": score, "confidence": "medium",
            "source": "orderbook+coingecko",
            "detail": detail}


def _score_smart_money(data: dict) -> dict:
    """
    4.5 聪明钱评分 (Smart Money Score)
    权重 20%

    使用订单簿买方/卖方深度 + 资金费率 + 价格行为综合判断。
    真实聪明钱流入流出需要 Nansen / 0xScope / Arkham 等链上标签数据。

    代理逻辑：
      - 深度买/卖比 + 资金费率方向 → 聪明钱偏向
      - 价格在关键支撑阻力附近的行为 → 聪明钱正在交易
    """
    symbol = data.get("symbol", "")
    ob = _fetch_binance_orderbook(symbol)
    funding = _fetch_funding_rate(symbol)
    ticker = _fetch_ticker_24h(symbol)

    if ob is None or ticker is None:
        return {"score": 50, "confidence": "low", "source": "default", "detail": "数据不足，默认中性"}

    price = ticker["price"]
    change_pct = ticker["change_pct"]
    high_24h = ticker["high"]
    low_24h = ticker["low"]

    score = 50
    signals = []

    # 判断：聪明钱通常在关键位置反向操作
    range_24h = high_24h - low_24h
    if range_24h > 0:
        range_pos = (price - low_24h) / range_24h
    else:
        range_pos = 0.5

    # 价格在高位但资金费率显示空头拥挤 → 聪明钱可能在做空
    if range_pos > 0.75 and funding and funding["signal"] == "short_crowded":
        score += 15
        signals.append("高价区+空头拥挤, 聪明钱可能做空")
    # 价格在低位但资金费率显示多头拥挤 → 聪明钱可能在做多
    elif range_pos < 0.25 and funding and funding["signal"] == "long_crowded":
        score += 15
        signals.append("低价区+多头拥挤, 聪明钱可能做多")
    # 价格在低位 + 空头拥挤 → 散户做空, 聪明钱吸筹
    elif range_pos < 0.25 and funding and funding["signal"] == "short_crowded":
        score += 20
        signals.append("低价区+空头拥挤, 聪明钱可能吸筹")

    # 订单簿深度信号
    bid_not = ob["bid_notional"]
    ask_not = ob["ask_notional"]
    depth_ratio = bid_not / ask_not if ask_not > 0 else 1

    if depth_ratio > 1.8:
        score += 15
        signals.append("买方深度远超卖方, 聪明钱在买入")
    elif depth_ratio > 1.3:
        score += 8
        signals.append("买方深度较优")
    elif depth_ratio < 0.6:
        score -= 15
        signals.append("卖方深度远超买方, 聪明钱可能出货")
    elif depth_ratio < 0.8:
        score -= 8
        signals.append("卖方深度略优")

    # 高涨幅+买方深度弱 → 拉高出货
    if change_pct > 15 and depth_ratio < 1.0:
        score -= 15
        signals.append(f"大涨({change_pct:+.1f}%)+卖方厚, 可能是拉高出货")

    # 高跌幅+买方深度强 → 洗盘吸筹
    if change_pct < -10 and depth_ratio > 1.5:
        score += 15
        signals.append(f"大跌({change_pct:+.1f}%)+买方厚, 可能是洗盘吸筹")

    score = max(10, min(95, score))
    detail = "; ".join(signals) if signals else "无明显聪明钱信号"
    conf = "medium" if signals else "low"
    return {"score": score, "confidence": conf,
            "source": "orderbook+funding",
            "detail": detail}


# ═══════════════════════════════════════════════════════════════════════
#  聚合评分
# ═══════════════════════════════════════════════════════════════════════

def calc_whale_score(symbol: str) -> dict:
    """
    计算 Whale Score 综合评分。

    返回:
      whale_score    : 0~100 综合分
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

    # 加权聚合
    ws = (
        exchange["score"] * WEIGHTS["exchange"]
        + holding["score"] * WEIGHTS["holding"]
        + transfer["score"] * WEIGHTS["transfer"]
        + concentration["score"] * WEIGHTS["concentration"]
        + smart_money["score"] * WEIGHTS["smart_money"]
    )

    # 置信度：各因子置信度的加权平均
    conf_map = {"high": 1.0, "medium": 0.7, "low": 0.4}
    overall_conf = 0.0
    for f in (exchange, holding, transfer, concentration, smart_money):
        conf_score = conf_map.get(f["confidence"], 0.5)
        overall_conf += conf_score
    overall_conf /= 5.0

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
        "factors": {
            "exchange": exchange,
            "holding": holding,
            "transfer": transfer,
            "concentration": concentration,
            "smart_money": smart_money,
        },
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
