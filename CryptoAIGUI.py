"""
CryptoAIGUI.py — 币安信号雷达桌面版
===================================
基于 PySide6 的桌面应用，调用 analyzer 模块分析币种。

用法:
  pip install PySide6
  python CryptoAIGUI.py
"""

import sys
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QTextEdit, QLabel, QStatusBar,
    QTabWidget, QFrame, QGridLayout
)
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QFont

from analyzer import analyze_coin_dict


# ── 样式常量 ────────────────────────────────────────────────────────────
CARD_STYLE = """
    QFrame#card {
        background-color: #0f3460;
        border: 1px solid #1a5276;
        border-radius: 8px;
        padding: 12px;
    }
"""
BTN_STYLE = """
    QPushButton {
        background-color: #2196F3;
        color: white;
        border: none;
        border-radius: 4px;
        font-size: 14px;
        font-weight: bold;
        padding: 8px 20px;
    }
    QPushButton:hover { background-color: #1976D2; }
    QPushButton:disabled { background-color: #90CAF9; }
"""
QUICK_BTN = """
    QPushButton {
        background-color: #37474F;
        color: #B0BEC5;
        border: 1px solid #546E7A;
        border-radius: 4px;
        font-size: 12px;
        padding: 4px 12px;
    }
    QPushButton:hover { background-color: #546E7A; color: white; }
"""
TAB_STYLE = """
    QTabWidget::pane {
        background-color: #16213e;
        border: 1px solid #1a5276;
        border-radius: 6px;
    }
    QTabBar::tab {
        background-color: #0f3460;
        color: #90CAF9;
        border: 1px solid #1a5276;
        border-bottom: none;
        border-radius: 4px 4px 0 0;
        padding: 8px 20px;
        font-size: 13px;
        margin-right: 2px;
    }
    QTabBar::tab:selected {
        background-color: #1a5276;
        color: white;
        font-weight: bold;
    }
    QTabBar::tab:hover:!selected {
        background-color: #1a1a3e;
    }
"""


# ── 后台分析线程 ──────────────────────────────────────────────────────
class Worker(QThread):
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


# ── 结果卡片组件 ──────────────────────────────────────────────────────
class ResultCard(QFrame):
    """专业风格的结果展示卡片"""

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self.setStyleSheet(CARD_STYLE)
        self._build_ui()

    def _build_ui(self):
        layout = QGridLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(16, 14, 16, 14)

        # 行 0: 币种 + 等级
        self.symbol_label = QLabel("—")
        self.symbol_label.setStyleSheet("font-size: 22px; font-weight: bold; color: #E3F2FD;")
        layout.addWidget(self.symbol_label, 0, 0)

        self.grade_label = QLabel("")
        self.grade_label.setStyleSheet("font-size: 20px; padding: 2px 10px; border-radius: 4px;")
        self.grade_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.grade_label, 0, 1)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #1a5276;")
        layout.addWidget(sep, 1, 0, 1, 2)

        # 行 2: 方向 + 置信度
        self.dir_label = QLabel("方向: —")
        self.dir_label.setStyleSheet("font-size: 16px; color: #B0BEC5;")
        layout.addWidget(self.dir_label, 2, 0)

        self.conf_label = QLabel("置信度: —")
        self.conf_label.setStyleSheet("font-size: 16px; color: #B0BEC5;")
        self.conf_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.conf_label, 2, 1)

        # 行 3: 入场价
        self.entry_label = QLabel("入场价: —")
        self.entry_label.setStyleSheet("font-size: 15px; color: #E0E0E0;")
        layout.addWidget(self.entry_label, 3, 0)

        # 行 4: 止损价
        self.sl_label = QLabel("止损价: —")
        self.sl_label.setStyleSheet("font-size: 15px; color: #EF9A9A;")
        layout.addWidget(self.sl_label, 4, 0)

        # 行 5: 止盈价
        self.tp_label = QLabel("止盈价: —")
        self.tp_label.setStyleSheet("font-size: 15px; color: #A5D6A7;")
        layout.addWidget(self.tp_label, 5, 0)

        # 行 6: 杠杆 + 仓位
        self.lev_label = QLabel("建议杠杆: —")
        self.lev_label.setStyleSheet("font-size: 14px; color: #90CAF9;")
        layout.addWidget(self.lev_label, 6, 0)

        self.pos_label = QLabel("仓位: —")
        self.pos_label.setStyleSheet("font-size: 14px; color: #90CAF9;")
        self.pos_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.pos_label, 6, 1)

        # 行 7: 24h涨跌
        self.price_label = QLabel("")
        self.price_label.setStyleSheet("font-size: 13px; color: #78909C;")
        layout.addWidget(self.price_label, 7, 0, 1, 2)

        self.setLayout(layout)

    def update_data(self, d: dict):
        """用分析结果填充卡片"""
        price = d.get("price", 0)
        symbol = d.get("symbol", "").replace("USDT", "")

        # 币种
        self.symbol_label.setText(f"{symbol}  ${price:,.4f}")

        # 等级
        grade = d.get("grade", "D")
        emoji = d.get("grade_emoji", "⚪")
        grade_colors = {
            "A": "background-color: #1B5E20; color: #C8E6C9;",
            "B": "background-color: #2E7D32; color: #C8E6C9;",
            "C": "background-color: #E65100; color: #FFE0B2;",
            "D": "background-color: #37474F; color: #90A4AE;",
        }
        grade_names = {"A": "强信号", "B": "中等信号", "C": "弱信号", "D": "无信号"}
        self.grade_label.setText(f"{emoji} {grade} — {grade_names.get(grade, '—')}")
        self.grade_label.setStyleSheet(f"font-size: 20px; padding: 2px 10px; border-radius: 4px; {grade_colors.get(grade, grade_colors['D'])}")

        # 方向
        direction = d.get("direction", "NEUTRAL")
        dir_colors = {"LONG": "#A5D6A7", "SHORT": "#EF9A9A", "NEUTRAL": "#B0BEC5"}
        self.dir_label.setText(f"方向: {d.get('direction_label', '⚪ 观望')}")
        self.dir_label.setStyleSheet(f"font-size: 16px; color: {dir_colors.get(direction, '#B0BEC5')};")

        # 置信度
        conf = d.get("confidence", 0)
        self.conf_label.setText(f"置信度: {conf:.1f}%")
        conf_color = "#C8E6C9" if conf >= 65 else "#FFE0B2" if conf >= 60 else "#B0BEC5"
        self.conf_label.setStyleSheet(f"font-size: 16px; color: {conf_color};")

        # 入场/止损/止盈
        entry_fmt = f"${d['entry_price']:,.6f}" if d.get("entry_price") else "—"
        sl_fmt = f"${d['stop_loss']:,.6f}  ({'-' if d['stop_loss'] else ''}{d.get('sl_pct', 0):.2f}%)" if d.get("stop_loss") else "—"
        tp_fmt = f"${d['take_profit']:,.6f}  ({'+' if d['take_profit'] else ''}{d.get('tp_pct', 0):.2f}%)" if d.get("take_profit") else "—"

        self.entry_label.setText(f"入场价:  {entry_fmt}")
        self.sl_label.setText(f"止损价:  {sl_fmt}")
        self.tp_label.setText(f"止盈价:  {tp_fmt}")

        # 杠杆+仓位
        lev = d.get("leverage", 0)
        pct = d.get("position_pct", 0)
        self.lev_label.setText(f"建议杠杆: {lev}x")
        self.pos_label.setText(f"仓位: {pct:.1f}%  (${d.get('notional_value', 0):,.2f})")

        # 24h价格变化
        change = d.get("change_24h", 0)
        h24 = d.get("high_24h", 0)
        l24 = d.get("low_24h", 0)
        chg_color = "#EF9A9A" if change < 0 else "#A5D6A7"
        self.price_label.setText(f"24h: <span style='color:{chg_color};'>{change:+.2f}%</span>  |  高 ${h24:,.2f}  低 ${l24:,.2f}")
        self.price_label.setTextFormat(Qt.TextFormat.RichText)


# ── 详细分析文本区 ──────────────────────────────────────────────────────
class DetailPanel(QTextEdit):
    """显示详细的指标理由和风险提示"""

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setFont(QFont("Consolas, Courier New, monospace", 11))
        self.setStyleSheet("""
            QTextEdit {
                background-color: #1a1a2e;
                color: #e0e0e0;
                border: 1px solid #333;
                border-radius: 6px;
                padding: 10px;
            }
        """)
        self.setPlaceholderText("详细分析信息将在分析完成后显示...")

    def update_data(self, d: dict):
        parts = []

        # ── 多空概率 ──
        lp = d.get("long_prob", 50)
        sp = d.get("short_prob", 50)
        long_bars = int(lp / 5)
        short_bars = int(sp / 5)
        parts.append("📊 多空概率")
        parts.append(f"   🟢 做多: {lp:.1f}%  {'■' * long_bars}")
        parts.append(f"   🔴 做空: {sp:.1f}%  {'■' * short_bars}")
        parts.append("")

        # ── 技术指标 ──
        parts.append("📈 技术指标")
        parts.append(f"   RSI(14): {d.get('rsi', '—')}  |  ATR: {d.get('atr_pct', 0):.2f}%  |  MACD: {d.get('macd_trend', '—')}")
        parts.append(f"   结构: {d.get('structure', '—')}  |  量比: x{d.get('volume_ratio', 0):.1f}  |  资金费率: {d.get('funding_rate', 0):+.6f}")
        parts.append(f"   共振: {'✅ 多TF一致' if d.get('tf_aligned') else '❌ TF分化'}")
        parts.append("")

        # ── 做多理由 ──
        reasons_l = d.get("reasons_long", [])
        if reasons_l:
            parts.append(f"🟢 做多理由 ({d.get('long_score', 0)}分)")
            for r in reasons_l:
                parts.append(f"   ✓ {r}")
            parts.append("")

        # ── 做空理由 ──
        reasons_s = d.get("reasons_short", [])
        if reasons_s:
            parts.append(f"🔴 做空理由 ({d.get('short_score', 0)}分)")
            for r in reasons_s:
                parts.append(f"   ✓ {r}")
            parts.append("")

        # ── 风险提示 ──
        warnings = d.get("warnings", [])
        if warnings:
            parts.append("⚠️ 风险提示")
            for w in warnings:
                parts.append(f"   · {w}")
            parts.append("")

        # ── 盈亏比 ──
        rr = d.get("rr_ratio")
        if rr:
            parts.append(f"📐 盈亏比: 1:{rr}")
            parts.append("")

        self.setText("\n".join(parts))


# ── 分析页面（Tab 1） ──────────────────────────────────────────────────
class AnalysisPage(QWidget):
    """分析页面：输入 + 结果卡片 + 详细分析"""

    def __init__(self):
        super().__init__()
        self._worker = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── 输入行 ──
        input_layout = QHBoxLayout()
        self.coin_input = QLineEdit()
        self.coin_input.setPlaceholderText("请输入币种，例如 BTC / ETH / SOL / OPG")
        self.coin_input.setMinimumHeight(36)
        self.coin_input.returnPressed.connect(self.run_analysis)

        self.analyze_btn = QPushButton("🔍 开始分析")
        self.analyze_btn.setMinimumHeight(36)
        self.analyze_btn.setMinimumWidth(120)
        self.analyze_btn.setStyleSheet(BTN_STYLE)

        input_layout.addWidget(self.coin_input, stretch=3)
        input_layout.addWidget(self.analyze_btn, stretch=1)
        layout.addLayout(input_layout)

        # ── 快捷按钮 ──
        quick_layout = QHBoxLayout()
        quick_layout.setSpacing(6)
        for coin in ["BTC", "ETH", "SOL", "OPG", "DOGE", "BNB"]:
            btn = QPushButton(coin)
            btn.setFixedHeight(28)
            btn.setStyleSheet(QUICK_BTN)
            btn.clicked.connect(lambda checked, c=coin: self._quick_coin(c))
            quick_layout.addWidget(btn)
        quick_layout.addStretch()
        layout.addLayout(quick_layout)

        # ── 结果卡片 ──
        self.card = ResultCard()
        layout.addWidget(self.card)

        # ── 详细分析 ──
        self.detail = DetailPanel()
        layout.addWidget(self.detail, stretch=1)

        # ── 状态标签 ──
        self.status_label = QLabel("就绪 ✅")
        self.status_label.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(self.status_label)

        self.setLayout(layout)

        # ── 信号绑定 ──
        self.analyze_btn.clicked.connect(self.run_analysis)

    def _quick_coin(self, coin: str):
        self.coin_input.setText(coin)
        self.run_analysis()

    def run_analysis(self):
        coin = self.coin_input.text().strip()
        if not coin:
            self.detail.setPlainText("⚠️ 请输入币种名称")
            return

        self.analyze_btn.setEnabled(False)
        self.analyze_btn.setText("⏳ 分析中...")
        self.status_label.setText(f"⏳ 正在获取 {coin.upper()} 数据...")

        self._worker = Worker(coin)
        self._worker.finished.connect(self._on_result)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_result(self, result: dict):
        self.card.update_data(result)
        self.detail.update_data(result)
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("🔍 开始分析")
        self.status_label.setText(f"✅ 完成 ({datetime.now():%H:%M:%S})")

    def _on_error(self, msg: str):
        self.detail.setPlainText(msg)
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("🔍 开始分析")
        self.status_label.setText("❌ 分析失败")


# ── 涨幅榜页面（Tab 2） ────────────────────────────────────────────────
class GainersPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout()
        label = QLabel("📈 涨幅榜")
        label.setStyleSheet("font-size: 18px; color: #A5D6A7; font-weight: bold;")
        layout.addWidget(label)
        hint = QLabel("功能开发中，敬请期待...")
        hint.setStyleSheet("color: #78909C; font-size: 14px;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint, stretch=1, alignment=Qt.AlignmentFlag.AlignCenter)
        self.setLayout(layout)


# ── 跌幅榜页面（Tab 3） ───────────────────────────────────────────────
class LosersPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout()
        label = QLabel("📉 跌幅榜")
        label.setStyleSheet("font-size: 18px; color: #EF9A9A; font-weight: bold;")
        layout.addWidget(label)
        hint = QLabel("功能开发中，敬请期待...")
        hint.setStyleSheet("color: #78909C; font-size: 14px;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint, stretch=1, alignment=Qt.AlignmentFlag.AlignCenter)
        self.setLayout(layout)


# ── 主窗口 ────────────────────────────────────────────────────────────
class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CryptoAI — 币安信号雷达")
        self.resize(720, 780)
        self.setMinimumSize(520, 580)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(8)

        # ── 标题 ──
        title = QLabel("🦐 币安超短线信号雷达")
        title_font = QFont()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # ── Tab 页 ──
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(TAB_STYLE)

        self.tabs.addTab(AnalysisPage(), "🔍 分析")
        self.tabs.addTab(GainersPage(), "📈 涨幅榜")
        self.tabs.addTab(LosersPage(), "📉 跌幅榜")

        layout.addWidget(self.tabs, stretch=1)
        self.setLayout(layout)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    app.setStyleSheet("""
        QWidget {
            background-color: #16213e;
            color: #e0e0e0;
        }
        QLineEdit {
            background-color: #0f3460;
            color: #e0e0e0;
            border: 1px solid #1a5276;
            border-radius: 4px;
            padding: 6px 10px;
            font-size: 14px;
        }
        QLineEdit:focus {
            border: 1px solid #2196F3;
        }
    """)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
