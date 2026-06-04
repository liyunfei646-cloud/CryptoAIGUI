"""
CryptoAIGUI.py — 币安信号雷达桌面版 (v2 UI)
=============================================
基于另一个 AI 设计的 React 前端外观，移植到 PySide6。
"""

import sys, math
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QTextEdit, QLabel, QFrame,
    QScrollArea, QSizePolicy, QGridLayout, QProgressBar
)
from PySide6.QtCore import QThread, Signal, Qt, QTimer
from PySide6.QtGui import QFont, QPixmap, QPainter, QColor, QLinearGradient, QBrush

from analyzer import analyze_coin_dict, fetch_movers


# ═══════════════════════════════════════════════════════════════════════
#  设计色板 (从 React UI 提取)
# ═══════════════════════════════════════════════════════════════════════

C = {
    "bg":         "#06122e",
    "header":     "#131e3b",
    "card":       "#0f3460",
    "card_rgb":   "15, 52, 96",
    "panel":      "#131e3b",
    "input":      "#0f1a37",
    "border":     "#1a5276",
    "text":       "#dae1ff",
    "text2":      "#bfc7d4",
    "text_muted": "#78909C",
    "accent":     "#9ecaff",
    "green":      "#a2d3a4",
    "red":        "#ffb4ab",
    "amber":      "#FFB74D",
    "dark_panel": "#0f1a37",
}

# ═══════════════════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════════════════

def _fmt_price(p: float) -> str:
    if p is None or math.isnan(p):
        return "—"
    if p < 0.01:
        return f"${p:.6f}"
    if p < 1:
        return f"${p:.4f}"
    return f"${p:,.2f}"

def _st(s: str) -> str:
    """快捷 stylesheet 注入"""
    return s

GRADE_NAMES = {"A": "GRADE S", "B": "GRADE A", "C": "GRADE B", "D": "GRADE C"}

# ═══════════════════════════════════════════════════════════════════════
#  后台线程
# ═══════════════════════════════════════════════════════════════════════

class AnalysisWorker(QThread):
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, symbol: str, balance: float = 1000.0):
        super().__init__()
        self.symbol = symbol
        self.balance = balance

    def run(self):
        try:
            result = analyze_coin_dict(self.symbol, self.balance)
            if "error" in result:
                self.error.emit(result["error"])
            else:
                self.finished.emit(result)
        except Exception as e:
            self.error.emit(f"❌ 分析异常: {e}")


class MoversWorker(QThread):
    finished = Signal(dict)

    def __init__(self, min_volume: float = 500_000, top_n: int = 20):
        super().__init__()
        self.min_volume = min_volume
        self.top_n = top_n

    def run(self):
        result = fetch_movers(self.min_volume, self.top_n)
        self.finished.emit(result)


# ═══════════════════════════════════════════════════════════════════════
#  结果卡片 — 完整移植 React ResultCard.tsx
# ═══════════════════════════════════════════════════════════════════════

class ResultCard(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        # ─── 主卡片 ──────────────────────────────────────────────────
        main_card = QFrame()
        main_card.setStyleSheet(f"""
            QFrame#mc {{
                background: rgba({C['card_rgb']}, 0.8);
                border: 1px solid {C['border']};
                border-radius: 12px;
            }}
        """)
        main_card.setObjectName("mc")

        mc_layout = QVBoxLayout(main_card)
        mc_layout.setContentsMargins(0, 0, 0, 0)
        mc_layout.setSpacing(0)

        # Header row: symbol + grade | price + change
        header = QFrame()
        header.setStyleSheet(f"background: rgba(30, 41, 70, 0.5); border-bottom: 1px solid {C['border']}80; border-radius: 12px 12px 0 0;")
        hdr = QHBoxLayout(header)
        hdr.setContentsMargins(20, 16, 20, 16)

        # Left: symbol
        self.sym_label = QLabel("BTC")
        self.sym_label.setStyleSheet(f"font-size: 24px; font-weight: bold; color: {C['text']};")
        hdr.addWidget(self.sym_label)

        hdr.addSpacing(8)

        # Grade badge
        self.grade_badge = QLabel("GRADE C")
        self.grade_badge.setStyleSheet(f"""
            font-size: 11px; font-weight: bold; padding: 2px 12px;
            border-radius: 4px;
            background: rgba({C['red'].replace('#', '')}, 0.2);
            color: {C['red']};
            border: 1px solid {C['red']}40;
        """)
        hdr.addWidget(self.grade_badge)

        hdr.addStretch()

        # Right: price + change
        right_col = QVBoxLayout()
        right_col.setSpacing(0)
        self.price_label = QLabel("$67,160.10")
        self.price_label.setStyleSheet(f"font-size: 18px; font-weight: bold; font-family: 'Consolas'; color: {C['text']};")
        self.price_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        right_col.addWidget(self.price_label)

        self.change_label = QLabel("▼ -3.35%")
        self.change_label.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {C['red']};")
        self.change_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        right_col.addWidget(self.change_label)

        hdr.addLayout(right_col)
        mc_layout.addWidget(header)

        # ─── Body: two-column grid ───────────────────────────────────
        body = QFrame()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(20, 20, 20, 20)
        body_layout.setSpacing(24)

        # -- Column A: Strategy --
        col_a = QVBoxLayout()
        col_a.setSpacing(16)

        # Direction
        dir_row = QHBoxLayout()
        dir_label = QLabel("建议方向")
        dir_label.setStyleSheet(f"font-size: 11px; font-weight: bold; text-transform: uppercase; color: {C['text2']}; letter-spacing: 1px;")
        dir_row.addWidget(dir_label)
        dir_row.addStretch()
        self.dir_value = QLabel("做多 (LONG)")
        self.dir_value.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {C['green']};")
        dir_row.addWidget(self.dir_value)
        col_a.addLayout(dir_row)

        # Confidence with bar
        conf_row = QHBoxLayout()
        conf_label = QLabel("置信度")
        conf_label.setStyleSheet(f"font-size: 11px; font-weight: bold; text-transform: uppercase; color: {C['text2']}; letter-spacing: 1px;")
        conf_row.addWidget(conf_label)
        conf_row.addStretch()

        self.conf_bar = QProgressBar()
        self.conf_bar.setFixedHeight(8)
        self.conf_bar.setFixedWidth(96)
        self.conf_bar.setTextVisible(False)
        self.conf_bar.setStyleSheet(f"""
            QProgressBar {{
                background: {C['dark_panel']}; border: none; border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {C['green']}, stop:1 #7bc67e);
                border-radius: 4px;
            }}
        """)
        conf_row.addWidget(self.conf_bar)

        self.conf_value = QLabel("57%")
        self.conf_value.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {C['green']};")
        conf_row.addWidget(self.conf_value)
        col_a.addLayout(conf_row)

        # Entry/Stop/TakeProfit panel
        esp = QFrame()
        esp.setStyleSheet(f"background: {C['card']}; border: 1px solid {C['border']}; border-radius: 8px;")
        esp_layout = QVBoxLayout(esp)
        esp_layout.setContentsMargins(12, 10, 12, 10)
        esp_layout.setSpacing(6)

        esp_refs = [
            ("esp_entry", "入场价", "entry_price", C['accent']),
            ("esp_tp", "止盈价", "take_profit", C['green']),
            ("esp_sl", "止损价", "stop_loss", C['red']),
        ]
        for attrname, label_text, _, color in esp_refs:
            row = QHBoxLayout()
            l = QLabel(label_text)
            l.setStyleSheet(f"font-size: 13px; color: {C['text2']};")
            row.addWidget(l)
            row.addStretch()
            v = QLabel("—")
            v.setStyleSheet(f"font-size: 13px; font-weight: bold; font-family: 'Consolas'; color: {color};")
            row.addWidget(v)
            esp_layout.addLayout(row)
            setattr(self, attrname, v)

        col_a.addWidget(esp)
        body_layout.addLayout(col_a)

        # -- Column B: Allocation & Stats --
        col_b = QVBoxLayout()
        col_b.setSpacing(12)

        stats = [
            ("st_lev", "杠杆倍数", "2x"),
            ("st_pos", "建议仓位", "117.6%"),
            ("st_notional", "名义价值", "$3211.50"),
        ]
        for attrname, label_text, _default in stats:
            row = QHBoxLayout()
            l = QLabel(label_text)
            l.setStyleSheet(f"font-size: 13px; color: {C['text2']};")
            row.addWidget(l)
            row.addStretch()
            v = QLabel(_default)
            v.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {C['text']};")
            row.addWidget(v)
            col_b.addLayout(row)
            setattr(self, attrname, v)

        # Separator
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {C['border']}40;")
        col_b.addWidget(sep)

        # 24h High / Low
        hl_refs = [("hl_high", "24h 最高"), ("hl_low", "24h 最低")]
        for attrname, label_text in hl_refs:
            row = QHBoxLayout()
            l = QLabel(label_text)
            l.setStyleSheet(f"font-size: 12px; color: {C['text2']};")
            row.addWidget(l)
            row.addStretch()
            v = QLabel("—")
            v.setStyleSheet(f"font-size: 12px; font-family: 'Consolas'; color: {C['text']};")
            row.addWidget(v)
            col_b.addLayout(row)
            setattr(self, attrname, v)

        body_layout.addLayout(col_b)
        mc_layout.addWidget(body)
        outer.addWidget(main_card)

        # ─── Bottom: 2-column indicators + logic ─────────────────────
        bottom = QHBoxLayout()
        bottom.setSpacing(10)

        # Left: Probability & Indicators
        self.indicator_panel = QFrame()
        self.indicator_panel.setStyleSheet(f"""
            background: rgba({C['card_rgb']}, 0.8);
            border: 1px solid {C['border']};
            border-radius: 12px;
        """)
        ind_layout = QVBoxLayout(self.indicator_panel)
        ind_layout.setContentsMargins(16, 14, 16, 14)
        ind_layout.setSpacing(12)

        ind_title = QLabel("概率预测与技术指标")
        ind_title.setStyleSheet(f"font-size: 11px; font-weight: bold; letter-spacing: 1px; color: {C['accent']}; border-left: 2px solid {C['accent']}; padding-left: 8px;")
        ind_layout.addWidget(ind_title)

        # Bull/Bear bars
        self.prob_bar_widget = QFrame()
        pb = QVBoxLayout(self.prob_bar_widget)
        pb.setSpacing(4)
        prob_labels = QHBoxLayout()
        self.bull_label = QLabel("多头 (—%)")
        self.bull_label.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {C['green']};")
        prob_labels.addWidget(self.bull_label)
        prob_labels.addStretch()
        self.bear_label = QLabel("空头 (—%)")
        self.bear_label.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {C['red']};")
        prob_labels.addWidget(self.bear_label)
        pb.addLayout(prob_labels)

        self.prob_bar = QFrame()
        self.prob_bar.setFixedHeight(12)
        self.prob_bar.setStyleSheet(f"background: {C['dark_panel']}; border: none; border-radius: 6px;")
        pb.addWidget(self.prob_bar)
        ind_layout.addWidget(self.prob_bar_widget)

        # 4-grid indicators
        ind_grid = QFrame()
        ig = QHBoxLayout(ind_grid)
        ig.setSpacing(8)

        self._ind_labels = {}
        for name, key in [("RSI (14)", "rsi"), ("ATR", "atr_pct"), ("MACD", "macd_trend"), ("量比", "volume_ratio")]:
            cell = QFrame()
            cell.setStyleSheet(f"background: {C['dark_panel']}; border: 1px solid {C['border']}40; border-radius: 6px;")
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(8, 8, 8, 8)
            cl.setSpacing(4)
            t = QLabel(name)
            t.setStyleSheet(f"font-size: 10px; text-transform: uppercase; font-weight: bold; color: {C['text2']};")
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(t)
            v = QLabel("—")
            v.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v.setStyleSheet(f"font-size: 14px; font-weight: bold; font-family: 'Consolas'; color: {C['text']};")
            cl.addWidget(v)
            self._ind_labels[key] = v
            ig.addWidget(cell)

        ind_layout.addWidget(ind_grid)
        bottom.addWidget(self.indicator_panel)

        # Right: Logic & Risk
        self.logic_panel = QFrame()
        self.logic_panel.setStyleSheet(f"""
            background: rgba({C['card_rgb']}, 0.8);
            border: 1px solid {C['border']};
            border-radius: 12px;
        """)
        logic_layout = QVBoxLayout(self.logic_panel)
        logic_layout.setContentsMargins(16, 14, 16, 14)
        logic_layout.setSpacing(8)

        logic_title = QLabel("多空逻辑与风控")
        logic_title.setStyleSheet(f"font-size: 11px; font-weight: bold; letter-spacing: 1px; color: {C['accent']}; border-left: 2px solid {C['accent']}; padding-left: 8px;")
        logic_layout.addWidget(logic_title)

        self.logic_text = QTextEdit()
        self.logic_text.setReadOnly(True)
        self.logic_text.setStyleSheet(f"""
            QTextEdit {{
                background: transparent; border: none; color: {C['text']};
                font-size: 12px; padding: 0;
            }}
        """)
        self.logic_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.logic_text.setFixedHeight(400)
        logic_layout.addWidget(self.logic_text)

        # Risk/Reward row
        rr_frame = QFrame()
        rr_frame.setStyleSheet(f"border-top: 1px solid {C['border']}40;")
        rr_layout = QHBoxLayout(rr_frame)
        rr_layout.setContentsMargins(0, 10, 0, 0)

        rr_left = QVBoxLayout()
        rr_l = QLabel("盈亏比 (R/R)")
        rr_l.setStyleSheet(f"font-size: 10px; text-transform: uppercase; font-weight: bold; color: {C['text2']};")
        rr_left.addWidget(rr_l)
        self.rr_value = QLabel("—")
        self.rr_value.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {C['green']};")
        rr_left.addWidget(self.rr_value)
        rr_layout.addLayout(rr_left)

        rr_layout.addStretch()

        rr_right = QVBoxLayout()
        risk_l = QLabel("风险评级")
        risk_l.setStyleSheet(f"font-size: 10px; text-transform: uppercase; font-weight: bold; color: {C['text2']};")
        risk_l.setAlignment(Qt.AlignmentFlag.AlignRight)
        rr_right.addWidget(risk_l)
        self.risk_badge = QLabel("—")
        self.risk_badge.setStyleSheet(f"""
            font-size: 11px; font-weight: bold; padding: 2px 8px;
            border-radius: 4px; background: rgba(162,211,164,0.2);
            color: {C['green']}; border: 1px solid {C['green']}30;
        """)
        self.risk_badge.setAlignment(Qt.AlignmentFlag.AlignRight)
        rr_right.addWidget(self.risk_badge)
        rr_layout.addLayout(rr_right)

        logic_layout.addWidget(rr_frame)
        bottom.addWidget(self.logic_panel)

        outer.addLayout(bottom)

    def update_data(self, d: dict):
        symbol = d.get("symbol", "").replace("USDT", "")
        grade = d.get("grade", "D")
        grade_name = GRADE_NAMES.get(grade, "GRADE C")

        self.sym_label.setText(symbol)

        price = d.get("price", 0)
        self.price_label.setText(_fmt_price(price))

        change = d.get("change_24h", 0)
        chg_color = C['green'] if change >= 0 else C['red']
        chg_arrow = "▲" if change >= 0 else "▼"
        self.change_label.setText(f"{chg_arrow} {change:+.2f}%")
        self.change_label.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {chg_color};")

        # Grade badge
        is_good = grade in ("A", "B")
        grade_bg = "rgba(162,211,164,0.2)" if is_good else f"rgba(255,180,171,0.2)"
        grade_col = C['green'] if is_good else C['red']
        self.grade_badge.setText(grade_name)
        self.grade_badge.setStyleSheet(f"""
            font-size: 11px; font-weight: bold; padding: 2px 12px;
            border-radius: 4px; background: {grade_bg};
            color: {grade_col}; border: 1px solid {grade_col}40;
        """)

        # Direction
        direction = d.get("direction", "NEUTRAL")
        is_long = direction == "LONG"
        dir_color = C['green'] if is_long else C['red'] if direction == "SHORT" else C['text2']
        dir_text = "做多 (LONG)" if is_long else "做空 (SHORT)" if direction == "SHORT" else "观望"
        self.dir_value.setText(dir_text)
        self.dir_value.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {dir_color};")

        # Confidence
        conf = d.get("confidence", 0)
        self.conf_value.setText(f"{conf:.0f}%")
        self.conf_bar.setValue(int(conf))
        conf_color = C['green'] if conf >= 65 else C['amber'] if conf >= 60 else C['text2']
        self.conf_value.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {conf_color};")

        # ESP values (stored references from _build)
        esp_keys = {"entry_price": "esp_entry", "take_profit": "esp_tp", "stop_loss": "esp_sl"}
        for dk, objname in esp_keys.items():
            w = getattr(self, objname, None)
            if w:
                val = d.get(dk)
                if val is not None:
                    w.setText(_fmt_price(val))

        # Stats
        stats_map = {"leverage": "st_lev", "position_pct": "st_pos", "notional_value": "st_notional"}
        for dk, objname in stats_map.items():
            w = getattr(self, objname, None)
            if w:
                val = d.get(dk, 0)
                if dk == "leverage":
                    w.setText(f"{val}x")
                elif dk == "position_pct":
                    w.setText(f"{val:.1f}%")
                elif dk == "notional_value":
                    w.setText(_fmt_price(val))

        # 24h HL
        hl_map = {"high_24h": "hl_high", "low_24h": "hl_low"}
        for dk, objname in hl_map.items():
            w = getattr(self, objname, None)
            if w:
                val = d.get(dk, 0)
                w.setText(_fmt_price(val))

        # Probability bars
        lp = d.get("long_prob", 50)
        sp = d.get("short_prob", 50)
        self.bull_label.setText(f"多头 ({lp:.0f}%)")
        self.bear_label.setText(f"空头 ({sp:.0f}%)")
        
        # Draw probability bar with QPainter would need override, use styled div
        total_w = max(1, lp + sp)
        bull_w = int(lp / total_w * 100)
        # Use a styled frame with two colored chunks
        self.prob_bar.setStyleSheet(f"""
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:{bull_w/100} {C['green']},
                stop:{bull_w/100+0.001} {C['red'] if sp > 0 else C['green']},
                stop:1 {C['red']});
            border-radius: 6px;
        """)

        # Indicator grid
        ind_map = {
            "rsi": (f"{d.get('rsi', '—')}", 
                    C['red'] if d.get('rsi', 50) > 70 else C['green'] if d.get('rsi', 50) < 30 else C['text']),
            "atr_pct": (f"{d.get('atr_pct', 0):.2f}%", C['text']),
            "macd_trend": (str(d.get('macd_trend', '—')).capitalize(), 
                          C['green'] if 'bull' in str(d.get('macd_trend','')) else C['red'] if 'bear' in str(d.get('macd_trend','')) else C['text']),
            "volume_ratio": (f"{d.get('volume_ratio', 0):.1f}x", C['amber']),
        }
        for key, (val, color) in ind_map.items():
            if key in self._ind_labels:
                self._ind_labels[key].setText(val)
                self._ind_labels[key].setStyleSheet(f"font-size: 14px; font-weight: bold; font-family: 'Consolas'; color: {color};")

        # Logic text — 详细版（类似脚本输出）
        parts = []

        # 技术指标
        ema20 = d.get("ema20", 0)
        ema50 = d.get("ema50", 0)
        rsi_val = d.get("rsi", "—")
        parts.append(f"📈 技术指标")
        parts.append(f"   RSI(14): {rsi_val}  |  EMA20: ${ema20:,.4f}  |  EMA50: ${ema50:,.4f}")
        parts.append(f"   结构: {d.get('structure','—')}  |  ATR: {d.get('atr_pct',0):.2f}%  |  MACD: {d.get('macd_trend','—').capitalize()}")
        funding = d.get('funding_rate', 0)
        aligned = d.get('tf_aligned', False)
        parts.append(f"   量比: x{d.get('volume_ratio',0):.1f}  |  资金费率: {funding:+.6f}  |  共振: {'✅' if aligned else '❌'}")
        fib = d.get('fib_nearest')
        if fib:
            parts.append(f"   Fib靠近: {fib}")
        rp = d.get('range_percentile')
        rm = d.get('rr_metric', '-')
        if rp is not None:
            parts.append(f"   区间百分位: {rp:.0f}%  |  S/R盈亏比: {rm}")
        support = d.get('support')
        resistance = d.get('resistance')
        if support and resistance:
            sup_d = d.get('sup_dist_pct', 0)
            res_d = d.get('res_dist_pct', 0)
            parts.append(f"   S/R: 支撑 ${support:,.4f} (-{sup_d}%)  |  阻力 ${resistance:,.4f} (+{res_d}%)")
        parts.append("")

        # 做多理由
        long_score = d.get("long_score", 0)
        long_reasons = d.get("reasons_long", [])
        parts.append(f"🟢 做多理由 ({long_score}分)")
        for r in long_reasons:
            parts.append(f"  ✓ {r}")
        parts.append("")

        # 做空理由
        short_score = d.get("short_score", 0)
        short_reasons = d.get("reasons_short", [])
        parts.append(f"🔴 做空理由 ({short_score}分)")
        for r in short_reasons:
            parts.append(f"  ✓ {r}")

        # 警告
        warnings = d.get("warnings", [])
        for w in warnings:
            parts.append(f"\n⚡ {w}")

        if len(parts) <= 2:
            parts.append("💡 正在计算指标多空强弱逻辑...")
        self.logic_text.setText("\n".join(parts))

        # RR + Risk
        rr = d.get("rr_ratio")
        risk_text = "低风险" if conf >= 75 else "中等风险" if conf >= 60 else "高风险"
        risk_colors = {
            "低风险": (C['green'], "rgba(162,211,164,0.2)", C['green']+"40"),
            "中等风险": (C['amber'], "rgba(255,183,77,0.2)", C['amber']+"40"),
            "高风险": (C['red'], f"rgba({C['red'].replace('#','')},0.2)", C['red']+"40"),
        }
        rc, rb, rborder = risk_colors.get(risk_text, (C['text2'], "", ""))
        self.rr_value.setText(f"{rr} : 1" if rr else "—")
        self.risk_badge.setText(risk_text)
        self.risk_badge.setStyleSheet(f"""
            font-size: 11px; font-weight: bold; padding: 2px 8px;
            border-radius: 4px; background: {rb};
            color: {rc}; border: 1px solid {rb};
        """)


# ═══════════════════════════════════════════════════════════════════════
#  分析页面
# ═══════════════════════════════════════════════════════════════════════

class AnalysisPage(QFrame):
    def __init__(self):
        super().__init__()
        self._worker = None
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(12)

        # Search bar
        search_row = QHBoxLayout()
        search_row.setSpacing(8)

        input_wrap = QFrame()
        input_wrap.setStyleSheet(f"""
            QFrame#sw {{
                background: {C['input']}; border: 1px solid {C['border']};
                border-radius: 12px; padding: 0;
            }}
            QFrame#sw:focus-within {{ border: 1px solid {C['accent']}; }}
        """)
        input_wrap.setObjectName("sw")
        iw = QHBoxLayout(input_wrap)
        iw.setContentsMargins(12, 0, 12, 0)

        search_icon = QLabel("🔍")
        search_icon.setStyleSheet(f"font-size: 14px; color: {C['text2']};")
        iw.addWidget(search_icon)

        self.coin_input = QLineEdit()
        self.coin_input.setPlaceholderText("输入币种代码 (例如: BTC, ETH...)")
        self.coin_input.setStyleSheet(f"""
            QLineEdit {{
                background: transparent; border: none; color: {C['text']};
                font-size: 14px; padding: 10px 4px;
            }}
            QLineEdit::placeholder {{ color: {C['text2']}60; }}
        """)
        self.coin_input.returnPressed.connect(self.run_analysis)
        iw.addWidget(self.coin_input, stretch=1)

        search_row.addWidget(input_wrap, stretch=3)

        self.analyze_btn = QPushButton("🔍 开始分析")
        self.analyze_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C['accent']}; color: #003258; font-weight: bold;
                border: none; border-radius: 12px; padding: 10px 24px;
                font-size: 14px;
            }}
            QPushButton:hover {{ background: #b0d4ff; }}
            QPushButton:disabled {{ background: #5a7a9e; }}
            QPushButton:pressed {{ padding-top: 12px; padding-bottom: 8px; }}
        """)
        self.analyze_btn.clicked.connect(self.run_analysis)
        search_row.addWidget(self.analyze_btn)
        layout.addLayout(search_row)

        # Quick coin chips
        chips_row = QHBoxLayout()
        chips_row.setSpacing(8)
        self._chip_btns = {}
        for coin in ["BTC", "ETH", "SOL", "OPG", "DOGE", "BNB"]:
            btn = QPushButton(coin)
            btn.setObjectName(f"chip_{coin}")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {C['panel']}; color: {C['text2']};
                    border: 1px solid {C['border']}; border-radius: 20px;
                    padding: 6px 16px; font-size: 12px; font-weight: 500;
                }}
                QPushButton:hover {{
                    border: 1px solid {C['accent']}80; color: {C['accent']};
                }}
            """)
            btn.clicked.connect(lambda checked, c=coin: self._quick_coin(c))
            chips_row.addWidget(btn)
            self._chip_btns[coin] = btn
        chips_row.addStretch()
        layout.addLayout(chips_row)

        # Result card
        self.card = ResultCard()
        layout.addWidget(self.card)

        # Status
        self.status_label = QLabel("就绪 ✅")
        self.status_label.setStyleSheet(f"color: {C['text_muted']}; font-size: 12px;")
        layout.addWidget(self.status_label)

        # Hide card initially
        self.card.hide()

    def _quick_coin(self, coin: str):
        self.coin_input.setText(coin)
        self._highlight_chip(coin)
        self.run_analysis()

    def _highlight_chip(self, coin: str):
        for name, btn in self._chip_btns.items():
            if name == coin:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {C['accent']}30; color: {C['accent']};
                        border: 1px solid {C['accent']}; border-radius: 20px;
                        padding: 6px 16px; font-size: 12px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {C['panel']}; color: {C['text2']};
                        border: 1px solid {C['border']}; border-radius: 20px;
                        padding: 6px 16px; font-size: 12px; font-weight: 500;
                    }}
                    QPushButton:hover {{ border: 1px solid {C['accent']}80; color: {C['accent']}; }}
                """)

    def analyze_coin(self, coin: str):
        self.coin_input.setText(coin)
        self._highlight_chip(coin)
        self.run_analysis()

    def run_analysis(self):
        coin = self.coin_input.text().strip()
        if not coin:
            return

        self.analyze_btn.setEnabled(False)
        self.analyze_btn.setText("⏳ 分析中...")
        self.status_label.setText(f"⏳ 正在调取量化引擎计算 {coin.upper()}...")
        self.card.hide()

        self._worker = AnalysisWorker(coin)
        self._worker.finished.connect(self._on_result)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_result(self, result: dict):
        self.card.update_data(result)
        self.card.show()
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("🔍 开始分析")
        self.status_label.setText(f"✅ 完成 ({datetime.now():%H:%M:%S})")

    def _on_error(self, msg: str):
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("🔍 开始分析")
        self.status_label.setText("❌ 分析失败")


# ═══════════════════════════════════════════════════════════════════════
#  币条目 (涨幅/跌幅榜用)
# ═══════════════════════════════════════════════════════════════════════

class CoinItem(QFrame):
    clicked = Signal(str)

    def __init__(self, rank: int, symbol: str, price: float,
                 change_pct: float, volume: float, is_gainer: bool):
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        coin_name = symbol.replace("USDT", "")
        chg_color = C['green'] if is_gainer else C['red']

        self.setStyleSheet(f"""
            QFrame#{coin_name} {{
                background: rgba(15, 52, 96, 0.7);
                border: 1px solid {C['border']}80;
                border-left: 2px solid transparent;
                border-radius: 8px;
            }}
            QFrame#{coin_name}:hover {{
                background: rgba(158, 202, 255, 0.08);
                border-left: 2px solid {C['accent']};
            }}
        """)
        self.setObjectName(coin_name)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        # Rank
        rl = QLabel(f"#{rank:02d}")
        rl.setFixedWidth(32)
        rl.setStyleSheet(f"font-size: 14px; font-family: 'Consolas'; color: {C['text2']};")
        layout.addWidget(rl)

        # Symbol
        nl = QLabel(coin_name)
        nl.setFixedWidth(80)
        nl.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {C['text']};")
        layout.addWidget(nl)

        nl2 = QLabel("/ USDT")
        nl2.setStyleSheet(f"font-size: 10px; color: {C['text2']}80;")
        layout.addWidget(nl2)

        layout.addStretch()

        # Price
        pl = QLabel(_fmt_price(price))
        pl.setFixedWidth(110)
        pl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        pl.setStyleSheet(f"font-size: 14px; font-family: 'Consolas'; color: {C['text']};")
        layout.addWidget(pl)

        # Change%
        cl = QLabel(f"{change_pct:+.2f}%")
        cl.setFixedWidth(80)
        cl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        cl.setStyleSheet(f"font-size: 14px; font-weight: bold; font-family: 'Consolas'; color: {chg_color};")
        layout.addWidget(cl)

        # Volume
        vl = QLabel(f"${volume:.1f}M")
        vl.setFixedWidth(72)
        vl.setStyleSheet(f"font-size: 12px; color: {C['text2']};")
        layout.addWidget(vl)

        # Analyze button
        ab = QPushButton("▶ 分析")
        ab.setStyleSheet(f"""
            QPushButton {{
                background: rgba(158,202,255,0.15); color: {C['accent']};
                border: 1px solid {C['accent']}30;
                border-radius: 4px; padding: 4px 10px;
                font-size: 11px; font-weight: bold;
            }}
            QPushButton:hover {{
                background: {C['accent']}; color: #003258;
            }}
        """)
        ab.clicked.connect(lambda: self.clicked.emit(coin_name))
        layout.addWidget(ab)

        # Whole row click
        self.mouseReleaseEvent = lambda e: self.clicked.emit(coin_name)


# ═══════════════════════════════════════════════════════════════════════
#  涨幅榜 / 跌幅榜
# ═══════════════════════════════════════════════════════════════════════

class MoversPageBase(QFrame):
    def __init__(self, title: str, emoji: str, title_color: str, is_gainer: bool, on_analyze=None):
        super().__init__()
        self.title = title
        self.emoji = emoji
        self.title_color = title_color
        self.is_gainer = is_gainer
        self.on_analyze = on_analyze
        self._worker = None
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        hdr = QFrame()
        hdr.setStyleSheet(f"background: rgba(19, 30, 59, 0.8); border-bottom: 1px solid {C['border']}40;")
        hdr_layout = QHBoxLayout(hdr)
        hdr_layout.setContentsMargins(16, 12, 16, 12)

        title_w = QLabel(f"{self.emoji} {self.title}")
        title_w.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {self.title_color};")
        hdr_layout.addWidget(title_w)

        badge = QLabel("REAL-TIME")
        badge.setStyleSheet(f"""
            font-size: 10px; font-weight: bold; padding: 2px 6px;
            border-radius: 4px; background: {self.title_color}15;
            color: {self.title_color}; border: 1px solid {self.title_color}20;
        """)
        hdr_layout.addWidget(badge)

        hdr_layout.addStretch()

        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C['card']}; color: {C['text']};
                border: 1px solid {C['border']}; border-radius: 8px;
                padding: 6px 12px; font-size: 12px;
            }}
            QPushButton:hover {{ border: 1px solid {C['accent']}; }}
            QPushButton:disabled {{ opacity: 0.5; }}
        """)
        self.refresh_btn.clicked.connect(self.refresh)
        hdr_layout.addWidget(self.refresh_btn)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"color: {C['text2']}; font-size: 11px;")
        hdr_layout.addWidget(self.status_label)

        layout.addWidget(hdr)

        # Column headers
        col_hdr = QFrame()
        col_hdr.setStyleSheet(f"background: {C['dark_panel']}; border-bottom: 1px solid {C['border']}40;")
        ch_layout = QHBoxLayout(col_hdr)
        ch_layout.setContentsMargins(12, 6, 12, 6)
        ch_layout.setSpacing(8)

        headers = [
            ("#", 32, "left"),
            ("币种", 110, "left"),
            ("", 30, "left"),
            ("价格 (USDT)", 110, "right"),
            ("24h 涨跌幅", 80, "right"),
            ("成交额", 72, "right"),
            ("", 70, "left"),
        ]
        for text, width, align in headers:
            l = QLabel(text)
            l.setFixedWidth(width)
            l.setStyleSheet(f"font-size: 12px; font-weight: 500; text-transform: uppercase; color: {C['text2']}; letter-spacing: 0.5px;")
            if align == "right":
                l.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            ch_layout.addWidget(l)

        layout.addWidget(col_hdr)

        # Scrollable list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ border: none; background: {C['bg']}; }}")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(12, 8, 12, 8)
        self.list_layout.setSpacing(4)
        self.list_layout.addStretch()

        scroll.setWidget(self.list_widget)
        layout.addWidget(scroll, stretch=1)

        # Hint
        self.hint_label = QLabel("点击「刷新」加载数据...")
        self.hint_label.setStyleSheet(f"color: {C['text_muted']}; font-size: 14px;")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list_layout.insertWidget(0, self.hint_label)

    def refresh(self):
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("⏳ 加载中...")
        self.status_label.setText("正在获取币安实时数据...")

        for i in reversed(range(self.list_layout.count())):
            item = self.list_layout.itemAt(i)
            if item.widget() and item.widget() != self.hint_label:
                item.widget().deleteLater()

        self._worker = MoversWorker()
        self._worker.finished.connect(self._on_data)
        self._worker.start()

    def _on_data(self, data: dict):
        self.refresh_btn.setEnabled(True)
        self.refresh_btn.setText("🔄 刷新")

        if data.get("error"):
            self.status_label.setText(f"❌ {data['error']}")
            self.hint_label.setText(f"加载失败: {data['error']}")
            self.hint_label.show()
            return

        items = data.get("gainers" if self.is_gainer else "losers", [])
        if not items:
            self.status_label.setText("暂无数据")
            self.hint_label.show()
            return

        self.hint_label.hide()

        for i, coin in enumerate(items):
            item = CoinItem(
                rank=i + 1,
                symbol=coin["symbol"],
                price=coin["price"],
                change_pct=coin["change_pct"],
                volume=coin["volume"],
                is_gainer=self.is_gainer,
            )
            item.clicked.connect(self._on_coin_click)
            self.list_layout.insertWidget(self.list_layout.count() - 1, item)

        self.status_label.setText(f"✅ {len(items)} 个币种  ({datetime.now():%H:%M})")

    def _on_coin_click(self, coin: str):
        if self.on_analyze:
            self.on_analyze(coin)


# ═══════════════════════════════════════════════════════════════════════
#  主窗口
# ═══════════════════════════════════════════════════════════════════════

APP_VERSION = "v1.2.0"

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CryptoAI — 币安超短线信号雷达")
        self.resize(750, 860)
        self.setMinimumSize(520, 640)
        self._current_tab = "analyze"
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ─── Header ──────────────────────────────────────────────────
        header = QFrame()
        header.setFixedHeight(64)
        header.setStyleSheet(f"background: {C['header']}; border-bottom: 1px solid {C['border']};")
        hdr = QHBoxLayout(header)
        hdr.setContentsMargins(20, 0, 20, 0)

        self.logo_label = QLabel("🦞 币安超短线信号雷达")
        self.logo_label.setStyleSheet(f"""
            font-size: 18px; font-weight: bold; color: {C['accent']};
        """)
        hdr.addWidget(self.logo_label)
        hdr.addStretch()

        # Desktop nav buttons (replacing QTabWidget)
        self._nav_btns = {}
        for tab_key, tab_text, tab_icon in [
            ("analyze", "分析", "🔍"),
            ("gainers", "涨幅榜", "📈"),
            ("losers", "跌幅榜", "📉"),
        ]:
            btn = QPushButton(f"{tab_icon} {tab_text}")
            btn.setObjectName(f"nav_{tab_key}")
            btn.setStyleSheet(self._nav_style(tab_key == "analyze"))
            btn.clicked.connect(lambda checked, k=tab_key: self._switch_tab(k))
            hdr.addWidget(btn)
            self._nav_btns[tab_key] = btn

        hdr.addSpacing(8)

        # Alert icon
        alert = QPushButton("🔔")
        alert.setFixedSize(36, 36)
        alert.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: none; border-radius: 18px;
                font-size: 16px; color: {C['text2']};
            }}
            QPushButton:hover {{ background: rgba(158,202,255,0.1); color: {C['accent']}; }}
        """)
        hdr.addWidget(alert)

        # Settings icon
        settings = QPushButton("⚙️")
        settings.setFixedSize(36, 36)
        settings.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: none; border-radius: 18px;
                font-size: 16px; color: {C['text2']};
            }}
            QPushButton:hover {{ background: rgba(158,202,255,0.1); color: {C['accent']}; }}
        """)
        hdr.addWidget(settings)

        outer.addWidget(header)

        # ─── Pages ──────────────────────────────────────────────────
        self.stack = QFrame()
        self.stack_layout = QVBoxLayout(self.stack)
        self.stack_layout.setContentsMargins(0, 0, 0, 0)

        self.analysis_page = AnalysisPage()
        self.gainers_page = MoversPageBase(
            title="涨幅榜 TOP 20", emoji="📈", title_color=C['green'],
            is_gainer=True, on_analyze=self._switch_to_analyze,
        )
        self.losers_page = MoversPageBase(
            title="跌幅榜 TOP 20", emoji="📉", title_color=C['red'],
            is_gainer=False, on_analyze=self._switch_to_analyze,
        )

        self.pages = {
            "analyze": self.analysis_page,
            "gainers": self.gainers_page,
            "losers": self.losers_page,
        }

        for p in self.pages.values():
            p.hide()
        self.pages["analyze"].show()
        self.stack_layout.addWidget(self.analysis_page)
        self.stack_layout.addWidget(self.gainers_page)
        self.stack_layout.addWidget(self.losers_page)

        outer.addWidget(self.stack, stretch=1)

        # ─── Footer ──────────────────────────────────────────────────
        footer = QFrame()
        footer.setFixedHeight(40)
        footer.setStyleSheet(f"background: {C['bg']}; border-top: 1px solid {C['border']};")
        ft = QHBoxLayout(footer)
        ft.setContentsMargins(16, 0, 16, 0)

        status_l = QLabel("就绪 ✅")
        status_l.setStyleSheet(f"font-size: 12px; color: {C['text_muted']};")
        ft.addWidget(status_l)
        ft.addSpacing(8)
        sep_ft = QFrame()
        sep_ft.setFixedWidth(1)
        sep_ft.setFixedHeight(12)
        sep_ft.setStyleSheet(f"background: {C['border']};")
        ft.addWidget(sep_ft)
        ft.addSpacing(8)
        sys_status = QLabel("System Status: Operational")
        sys_status.setStyleSheet(f"font-size: 12px; color: {C['text_muted']};")
        ft.addWidget(sys_status)

        ft.addStretch()

        ver = QLabel(APP_VERSION)
        ver.setStyleSheet(f"font-size: 12px; color: {C['text_muted']};")
        ft.addWidget(ver)

        live_dot = QLabel("●")
        live_dot.setStyleSheet(f"font-size: 10px; color: {C['accent']};")
        ft.addWidget(live_dot)
        live_text = QLabel("LIVE CONNECTED")
        live_text.setStyleSheet(f"font-size: 11px; font-weight: bold; color: {C['accent']};")
        ft.addWidget(live_text)

        outer.addWidget(footer)

        # ─── Background glow effects ─────────────────────────────────
        self.setStyleSheet(f"""
            MainWindow {{
                background: {C['bg']};
            }}
        """)

        # Update timer for blinking indicators
        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    def _nav_style(self, active: bool) -> str:
        if active:
            return f"""
                QPushButton {{
                    background: transparent; color: {C['accent']};
                    border: none; border-bottom: 2px solid {C['accent']};
                    font-size: 14px; font-weight: bold; padding: 0 12px;
                    padding-bottom: 4px;
                }}
            """
        else:
            return f"""
                QPushButton {{
                    background: transparent; color: {C['text2']};
                    border: none; font-size: 14px; padding: 0 12px;
                    padding-bottom: 4px;
                }}
                QPushButton:hover {{ color: {C['accent']}; }}
            """

    def _switch_tab(self, tab: str):
        self._current_tab = tab
        for key, page in self.pages.items():
            page.setVisible(key == tab)
        for key, btn in self._nav_btns.items():
            btn.setStyleSheet(self._nav_style(key == tab))

    def _switch_to_analyze(self, coin: str):
        self._switch_tab("analyze")
        self.analysis_page.analyze_coin(coin)

    def _tick(self):
        """1秒心跳，刷新实时指示器"""
        pass


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Global font
    font = QFont("Inter, 'Segoe UI', system-ui, sans-serif", 10)
    app.setFont(font)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
