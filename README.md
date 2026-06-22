# 🦐 CryptoAIGUI — 币安超短线信号雷达桌面版

基于 **PySide6** 的币安超短线交易分析桌面应用。输入币种，一键获取多维度技术分析 + 巨鲸评分。

## 功能

- 🔍 输入任意 USDT 交易对，运行七维交叉验证分析
- 🐋 集成 Whale Score 巨鲸评分（5因子 × 加权聚合）
- ⚡ 内置快捷按钮（BTC/ETH/SOL/OPG/DOGE/BNB）
- 🌙 深色主题，沉浸式体验
- 🧵 后台线程分析，界面不卡死

## 分析维度

| 维度 | 权重 | 说明 |
|---|---|---|
| EMA排列 | — | 9/21/55 三均线方向与多空排列 |
| MACD动量 | — | DIFF/DEA 金叉死叉 + 柱体衰减 + 背离 |
| RSI背离 | — | 14周期 RSI + 顶底背离检测 |
| 量价关系 | — | 成交量异常与价格背离 |
| K线形态 | — | 吞没、十字星、锤子线等 |
| 支撑阻力 | — | 近期高低点自动识别 + 斐波那契 |
| 均线偏离度 | — | 价格偏离 EMA 的程度 |
| **Whale Score** | **20%** | 巨鲸评分（交易所资金流/持仓变化/大额转账/集中度/聪明钱） |

## Whale Score 巨鲸评分

基于 [WhaleScore_V1.md](/root/WhaleScore_V1.md) 设计文档实现，5因子加权聚合：

| 因子 | 权重 | 数据源 |
|---|---|---|
| 交易所资金流 (Exchange Score) | 30% | Binance 订单簿买卖压力 |
| 巨鲸持仓变化 (Holding Score) | 25% | 成交额/市值比 + 资金费率 |
| 大额转账活跃度 (Transfer Score) | 15% | 撮合频次 + 平均单笔成交额 |
| 持仓集中度 (Concentration Score) | 10% | 订单簿分布 + CoinGecko Rank |
| 聪明钱行为 (Smart Money Score) | 20% | 订单簿深度比 + 资金费率方向 + 价格位置 |

> 链上数据（交易所净流入、巨鲸地址数等）需要付费 API（Glassnode/Nansen）。
> 当前版本使用 **交易所级代理指标** 估算，置信度标注为 `medium`/`low`。
> 当真实数据源可用时，只需替换 `whale_score_engine.py` 的数据获取层。

集成方式（符合文档 FinalScore 公式）：
```
FinalScore = Technical×0.50 + FundRate×0.15 + OI×0.15 + WhaleScore×0.20
```
Whale Score 在评分系统中做方向一致性检查，不一致时仓位减半。

## 快速开始

```bash
pip install PySide6>=6.6
python CryptoAIGUI.py
```

## 文件结构

```
CryptoAIGUI.py              # GUI 主程序（前端）
analyzer.py                 # 分析引擎（后端）
whale_score_engine.py       # 🐋 巨鲸评分引擎（独立模块）
binance_scalping_analyzer.py  # 独立版 CLI 分析脚本
WhaleScore_V1.md            # 巨鲸评分设计文档
requirements-gui.txt        # 依赖
```

## 依赖

- Python 3.10+
- PySide6 >= 6.6
- 无需外部 API Key（使用 CoinGecko / Binance 免费 API）
