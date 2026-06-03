# 🦐 CryptoAIGUI — 币安超短线信号雷达桌面版

基于 **PySide6** 的币安超短线交易分析桌面应用。输入币种，一键获取多维度技术分析信号。

## 功能

- 🔍 输入任意 USDT 交易对，运行七维交叉验证分析
- ⚡ 内置快捷按钮（BTC/ETH/SOL/OPG/DOGE/BNB）
- 🌙 深色主题，沉浸式体验
- 🧵 后台线程分析，界面不卡死

## 分析维度

| 维度 | 说明 |
|---|---|
| EMA排列 | 9/21/55 三均线方向与多空排列 |
| MACD动量 | DIFF/DEA 金叉死叉 + 柱体衰减 |
| RSI背离 | 14周期 RSI + 顶底背离检测 |
| 量价关系 | 成交量异常与价格背离 |
| K线形态 | 吞没、十字星、锤子线等 |
| 支撑阻力 | 近期高低点自动识别 |
| 均线偏离度 | 价格偏离 EMA 的程度 |

## 快速开始

```bash
pip install PySide6>=6.6
python CryptoAIGUI.py
```

## 文件结构

```
CryptoAIGUI.py              # GUI 主程序（前端）
analyzer.py                 # 分析引擎（后端）
binance_scalping_analyzer.py  # 独立版 CLI 分析脚本
requirements-gui.txt        # 依赖
```

## 依赖

- Python 3.10+
- PySide6 >= 6.6
