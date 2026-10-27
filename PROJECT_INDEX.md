# PROJECT_INDEX — 文件地图与作用说明

> 生成：2026-10-27 ｜ 基线：`d581330`（V3.1 P1 收口）｜ 维护规则：新增/重命名文件必须同步更新本文件
>
> **项目当前状态：V3.1 验收 8/8 未通过 → 4h 做空系统实盘闸门为 `NO TRADE`。**
> 本仓库中「生产」= 会给用户输出结论；「研究」= 只用于验证/证伪，不产生交易许可。

---

## 0. 一图看懂调用关系

```
┌─ GUI 桌面版（生产展示） ────────────────────────────────┐
│ CryptoAIGUI.py  →  ui.py  →  analyzer.py                │
│                                  ├─ futures_data.py     │
│                                  └─ whale_score_engine.py│
│                                  └─ strategy.py         │
└─────────────────────────────────────────────────────────┘

┌─ QQ 机器人（生产输出） ─────────────────────────────────┐
│ coin_analysis_4h.py  ←  v31_gate.json（实盘闸门）        │
│        └─ 复用 futures_data / 4h 因子逻辑                │
└─────────────────────────────────────────────────────────┘

┌─ 研究/验证（不产生交易许可） ───────────────────────────┐
│ backtest_v31.py  →  backtest_v31_report*.json            │
│        └→ build_gate.py → v31_gate.json                 │
│ backtest_4h.py / backtest_4h_tech.py / backtest_4h_scan.py│
│ backtest_v2.py                                          │
│ signal_audit.py → signals/*.jsonl（样本积累）             │
└─────────────────────────────────────────────────────────┘
```

---

## 1. 程序入口与前端（生产）

| 文件 | 作用 | 关键内容 | 状态 |
|---|---|---|---|
| `CryptoAIGUI.py` | 桌面版启动入口（仅启动器） | `main()`：加载 QApplication + `MainWindow` | 🟢 生产 |
| `ui.py` | PySide6 前端全部组件 | `MainWindow` / `AnalysisPage` / `MoversPage` / `AnalysisWorker` / `MoversWorker` / `ResultCard`；后台线程调用 analyzer，界面不阻塞 | 🟢 生产 |
| `requirements-gui.txt` | GUI 依赖清单 | `PySide6>=6.6` | 🟢 生产 |

启动：

```bash
pip install -r requirements-gui.txt
python CryptoAIGUI.py
```

---

## 2. 分析引擎（生产）

| 文件 | 作用 | 关键内容 | 状态 |
|---|---|---|---|
| `analyzer.py` | GUI 的分析引擎，也是 V2.1 信号定义的**唯一真源**（回测与实时共用） | `analyze_coin()` / `analyze_coin_dict()` 对外入口；指标：EMA/RSI/MACD/ATR/结构/斐波那契/K线形态/量能；`score_system()` 产出 `READY / WAIT / NO_TRADE` 状态机 + `candidate/trade_direction` 分离 + 伪概率标注 `score_implied`；`format_gui_details()` 负责展示文本；`risk_recommendation()` 仅在 READY 时给 entry/SL/TP；15m/5m 只吃 **closed_only** K线 | 🟢 生产 |
| `strategy.py` | V2.1 策略层：把「市场状态」翻译成「交易假设」 | 四大策略 `TREND_CONTINUATION` / `LIQUIDITY_REVERSAL` / `RANGE_REVERSION` / `BREAKOUT`；regime×策略允许矩阵；`TRADE_DOMAIN`（默认 `trend_up_long`）；`domain_allows()` 域外→NO_TRADE；`required_confirm_count()` 顺势2/逆势3/高波动+1 | 🟢 生产（研究阶段配置） |
| `futures_data.py` | 合约衍生品数据层 + Market Regime 检测 | OI（当前 + 5m/15m/1h 变化）、Funding（8h 结算序列/趋势）、Taker Buy/Sell、全市场多空比、近期强平；`detect_market_regime()` 用 4h/1h 双层判趋势/震荡/波动；**OI 历史硬上限 ≈20.8 天**（`limit=500`×1h） | 🟢 生产 |
| `whale_score_engine.py` | 巨鲸评分引擎 Whale Score V2.0 | 5 因子加权：exchange（Taker 资金流）/ holding（OI+Funding）/ transfer / concentration / smart_money / orderbook；V2.0 做了**数据源去重**（旧版 3 个因子共读订单簿→重复计分）；带本地缓存，无付费 API 依赖 | 🟢 生产 |
| `binance_scalping_analyzer.py` | 早期独立 CLI 分析脚本（六层过滤：趋势→结构→斐波那契→K线→量能→风控） | 零外部依赖、纯标准库；是 `analyzer.py` 的前身，逻辑已大部分被吸收 | 🔴 归档（保留做对照，不再演进） |

---

## 3. QQ 机器人输出（生产）

| 文件 | 作用 | 关键内容 | 状态 |
|---|---|---|---|
| `coin_analysis_4h.py` | QQ 场景的 4h 做空信号检查脚本，stdout 即用户看到的全文 | 10 个纯技术做空因子（`compute_short_factors`，可选跳过衍生品）；`leverage_for_atr()` ATR 阶梯杠杆；`structural_sl_tp()` 结构止损/动态 TP；**读取 `v31_gate.json` 输出「统计验证 + Trade Permission」段落**——当前 4 币 `verdict=false` → 一律 `⛔ NO TRADE`；`MAX_CHARS` 保头保尾截断防超长 | 🟢 生产 |

用法：

```bash
python3 coin_analysis_4h.py SPCX      # 自动补 USDT
```

> QQ 触发规则见 `AGENTS.md`：`分析XXX` 或 2-8 字符纯币名 → 原样输出 stdout，禁止加任何 AI 文字。

---

## 4. 回测与验证脚本（研究，不产生交易许可）

| 文件 | 作用 | 决策周期 | 关键口径 | 状态 |
|---|---|---|---|---|
| `backtest_v31.py` | **V3.1 Alpha 验证框架（当前唯一权威口径）** | 4h 决策 + 5m 追踪 | 成本三档（乐观/基准/保守，按生命周期扣手续费+Spread+Slippage+Funding 事件）；随机化 Mode A 独立随机方向 / Mode B 保比例置换 / Mode B′ 随机入场时点；基线 Trend/Buy&Hold/Short&Hold；Walk-Forward 30 训练/15 测试/15 滚动；Bootstrap 95% CI；Regime 分桶；同根 SL/TP 都触及判 SL；最大持仓 64h；环境变量 `MIN_SIG` / `MIN_CLUSTER` / `DEDUP` / `SPAN_MODE` | 🟡 研究（权威） |
| `backtest_4h.py` | 4h 做空系统完整 SL/TP 盈亏曲线（含衍生品因子注入） | 4h 决策 + 5m 追踪 | 决策区间被 OI 历史卡在 ~20.8 天；方案 A 固定 ATR、方案 B 结构止损；最大持仓 16 根 4h；输出 `backtest_4h_<SYM>_<N>d.json` | 🟡 研究 |
| `backtest_4h_tech.py` | 4h 做空系统长周期**纯技术面**回测（跳过 OI/Funding 两因子，拉到 60-180 天） | 4h 决策 + 5m 追踪 | 回答「20 天正期望是真 edge 还是行情 beta」；⚠️ **旧版 `days` 只扣 warmup 不截窗（"90 天"实际 ~114 天），该口径已作废**，仅用于复现旧表 | 🔴 归档（口径已废弃，勿引用其数字） |
| `backtest_4h_scan.py` | 4h 系统 SL/TP 乘数网格扫描 | 4h 决策 | 6×5 网格 + 无止损基准；用于判断「信号失效」还是「参数失配」 | 🟡 研究 |
| `backtest_v2.py` | V2.1 短线系统回测（1h 决策） | **1h 决策** + 5m 追踪 | 只有 `signal_status=READY` 才计入（WAIT 不算 LOSS）；成本 0.15%/笔；`--experiment` 消融（baseline / no_whale / trend_only / reversal_only / range_only / breakout_only）；`--tech-only` 长周期降级；输出 `backtest_<SYM>_<N>d_<experiment>.json` | 🟡 研究 |
| `signal_audit.py` | 信号审计：每次信号存全字段快照，事后回填结果 | — | 记录 +5m/+15m/+30m/+60m/+4h 的 MFE/MAE/SL/TP/收益；存储 `signals/signals_pending.jsonl` / `signals_done.jsonl`；只评估已收盘 K线，无未来函数；`stats()` 出分桶胜率 | 🟡 研究（样本积累） |

常用命令：

```bash
python3 backtest_v31.py BTCUSDT,ETHUSDT,SOLUSDT,SPCXUSDT 90
MIN_SIG=4 python3 backtest_v31.py BTCUSDT 90
python3 backtest_4h.py SPCXUSDT 20 1.0 1.0
python3 backtest_4h_tech.py SPCXUSDT 90 1.0 1.0
python3 backtest_4h_scan.py SPCXUSDT 20
python3 backtest_v2.py --tech-only BTCUSDT 60
```

---

## 5. 实盘闸门（生产，硬约束）

| 文件 | 作用 | 关键内容 | 状态 |
|---|---|---|---|
| `build_gate.py` | 从 V3.1 报告生成闸门数据 | 读 `backtest_v31_report*.json`，逐币判定：`Net PF>1` 且 `CI95 下界>1` 且 `置换 p<0.05` 且 `优于 Trend 基线` → TRADE，否则 `verdict=false` | 🟢 生产（由研究驱动） |
| `v31_gate.json` | 闸门结果快照，被 `coin_analysis_4h.py` 读取 | 4 币：BTC/ETH/SOL/SPCX 全部 `verdict=false`；含 n / 胜率 / GrossPF / NetPF / CI / EV / 成本 / TrendPF / p 值 / 生成时间 | 🟢 生产 |

> 规则来源：9/20 立法「策略须回测证明正期望才可实盘」+ V3.1 §13 八条验收。

---

## 6. 文档

| 文件 | 作用 | 何时看 |
|---|---|---|
| `README.md` | GUI 桌面版使用说明：功能、分析维度、Whale Score 公式、快速开始 | 第一次跑 GUI |
| `V31_REPORT.md` | V3.1 首轮验证报告：OOS 主结果、WF、基线对照、成本敏感性、Regime 分桶、规范偏离 | 想知道「数字是怎么算出来的」 |
| `V31_VERDICT.md` | **V3.1 验收结论书（权威判词）**：8/8 未通过、五问回答、主结果表、取证、残留 P1；附录 A 为 P1 去相关复核（SPCX GrossPF 1.11→0.85） | 想知道「为什么 NO TRADE」 |
| `PROJECT_INDEX.md` | 本文件：每个文件干什么、怎么跑、哪些已作废 | 接手/改代码前 |

---

## 7. 数据文件（`backtest_*.json`）

> 注意：`.gitignore` 已忽略 `backtest_*.json`，下面这些是**历史上已跟踪**的样本文件；新回测产物默认不入库。

| 文件 | 产出脚本 | 内容 |
|---|---|---|
| `backtest_BTCUSDT_20d.json` | `backtest_4h.py` | 4h 系统 20 天逐笔明细（SL/TP 曲线） |
| `backtest_4h_tech_{BTC,ETH,SOL,SPCX}USDT_90d.json` | `backtest_4h_tech.py` | 4h 纯技术面 90 天逐笔（⚠️ 旧 114 天口径，仅存档） |
| `backtest_{SYM}_20d_trend_up_long.json` | `backtest_v2.py`（experiment=`trend_up_long`） | V2.1 顺势做多域 20 天明细 |
| `backtest_{SYM}_3d_baseline.json` | `backtest_v2.py` | 3 天 baseline 冒烟测试 |
| `backtest_{SYM}_60d_baseline.json` | `backtest_v2.py --tech-only` | 60 天纯技术面 baseline |
| `backtest_v31_report*.json` | `backtest_v31.py` | 仅本地保留（体积大，不入库）：`_sig3` / `_sig4` / `_dedup2` / `_dedup3` / `_legacy` |
| `v31_gate.json` | `build_gate.py` | 闸门结果（**入库**，生产依赖） |

`signals/` — 运行时审计存储（JSONL + cron.log），已 gitignore，不入库。

---

## 8. 状态图例

| 标记 | 含义 |
|---|---|
| 🟢 生产 | 会进入用户可见输出或实盘判定，改动需谨慎并跑回归 |
| 🟡 研究 | 只用于验证/证伪，结论需经 V3.1 全套检验才可升级 |
| 🔴 归档 | 保留供复现/对照，数字与代码均不应作为当前依据 |

---

## 9. 不在本仓库的东西（重要）

| 内容 | 位置 | 说明 |
|---|---|---|
| API 密钥 | 本地明文文件（如 `binance_api_keys.md`） | **禁止入库**；仓库为公开仓库，`.gitignore` 已加 `*api_keys*`/`*keys*.json`/`.env` |
| 长期记忆/会话状态 | `~/.openclaw/workspace/`（OpenClaw 工作区） | 与代码仓库分离 |
| 大体积回测报告 | 本地 `backtest_v31_report*.json` | 需要共享时走 Release 或对象存储，不进 main |
| 运行日志 | `signals/`、`*.log` | 已忽略 |

---

## 10. 改动流程（强制）

1. 在 **本仓库**（`/root/CryptoAIGUI`）修改，不在运行副本里改
2. `git add` → `git commit` → `git push origin main`
3. 推送成功后再给结论；涉及生产脚本（`analyzer.py` / `coin_analysis_4h.py` / `v31_gate.json`）需重跑对应回测或冒烟验证
4. 新增/删除文件 → 同步更新本文件
