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
    QTabWidget, QFrame, QGridLayout, QScrollArea, QSizePolicy
)
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QFont

from analyzer import analyze_coin_dict, fetch_movers


# ── 样式 ────────────────────────────────────────────────────────────────
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
SMALL_BTN = """
    QPushButton {
        background-color: #1a5276;
        color: #90CAF9;
        border: 1px solid #1565C0;
        border-radius: 3px;
        font-size: 11px;
        padding: 3px 10px;
    }
    QPushButton:hover { background-color: #1565C0; color: white; }
"""
REFRESH_BTN = """
    QPushButton {
        background-color: #2E7D32;
        color: white;
        border: none;
        border-radius: 4px;
        font-size: 12px;
        font-weight: bold;
        padding: 6px 16px;
    }
    QPushButton:hover { background-color: #388E3C; }
    QPushButton:disabled { background-color: #558B2F; }
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
COIN_ITEM_STYLE = """
    QFrame#coin_item {
        background-color: #0d2137;
        border: 1px solid #1a3a5c;
        border-radius: 6px;
        padding: 8px;
    }
    QFrame#coin_item:hover {
        background-color: #122a45;
        border: 1px solid #2196F3;
    }
"""


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
#  分析结果卡片
# ═══════════════════════════════════════════════════════════════════════

class ResultCard(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self.setStyleSheet(CARD_STYLE)
        self._build_ui()

    def _build_ui(self):
        layout = QGridLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(16, 14, 16, 14)

        self.symbol_label = QLabel("—")
        self.symbol_label.setStyleSheet("font-size: 22px; font-weight: bold; color: #E3F2FD;")
        layout.addWidget(self.symbol_label, 0, 0)

        self.grade_label = QLabel("")
        self.grade_label.setStyleSheet("font-size: 20px; padding: 2px 10px; border-radius: 4px;")
        self.grade_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.grade_label, 0, 1)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #1a5276;")
        layout.addWidget(sep, 1, 0, 1, 2)

        self.dir_label = QLabel("方向: —")
        self.dir_label.setStyleSheet("font-size: 16px; color: #B0BEC5;")
        layout.addWidget(self.dir_label, 2, 0)

        self.conf_label = QLabel("置信度: —")
        self.conf_label.setStyleSheet("font-size: 16px; color: #B0BEC5;")
        self.conf_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.conf_label, 2, 1)

        self.entry_label = QLabel("入场价: —")
        self.entry_label.setStyleSheet("font-size: 15px; color: #E0E0E0;")
        layout.addWidget(self.entry_label, 3, 0)

        self.sl_label = QLabel("止损价: —")
        self.sl_label.setStyleSheet("font-size: 15px; color: #EF9A9A;")
        layout.addWidget(self.sl_label, 4, 0)

        self.tp_label = QLabel("止盈价: —")
        self.tp_label.setStyleSheet("font-size: 15px; color: #A5D6A7;")
        layout.addWidget(self.tp_label, 5, 0)

        self.lev_label = QLabel("建议杠杆: —")
        self.lev_label.setStyleSheet("font-size: 14px; color: #90CAF9;")
        layout.addWidget(self.lev_label, 6, 0)

        self.pos_label = QLabel("仓位: —")
        self.pos_label.setStyleSheet("font-size: 14px; color: #90CAF9;")
        self.pos_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.pos_label, 6, 1)

        self.price_label = QLabel("")
        self.price_label.setStyleSheet("font-size: 13px; color: #78909C;")
        layout.addWidget(self.price_label, 7, 0, 1, 2)

        self.setLayout(layout)

    def update_data(self, d: dict):
        price = d.get("price", 0)
        symbol = d.get("symbol", "").replace("USDT", "")

        self.symbol_label.setText(f"{symbol}  ${price:,.4f}")

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

        direction = d.get("direction", "NEUTRAL")
        dir_colors = {"LONG": "#A5D6A7", "SHORT": "#EF9A9A", "NEUTRAL": "#B0BEC5"}
        self.dir_label.setText(f"方向: {d.get('direction_label', '⚪ 观望')}")
        self.dir_label.setStyleSheet(f"font-size: 16px; color: {dir_colors.get(direction, '#B0BEC5')};")

        conf = d.get("confidence", 0)
        self.conf_label.setText(f"置信度: {conf:.1f}%")
        conf_color = "#C8E6C9" if conf >= 65 else "#FFE0B2" if conf >= 60 else "#B0BEC5"
        self.conf_label.setStyleSheet(f"font-size: 16px; color: {conf_color};")

        entry_fmt = f"${d['entry_price']:,.6f}" if d.get("entry_price") else "—"
        sl_fmt = f"${d['stop_loss']:,.6f}  ({d.get('sl_pct', 0):.2f}%)" if d.get("stop_loss") else "—"
        tp_fmt = f"${d['take_profit']:,.6f}  ({d.get('tp_pct', 0):.2f}%)" if d.get("take_profit") else "—"

        self.entry_label.setText(f"入场价:  {entry_fmt}")
        self.sl_label.setText(f"止损价:  {sl_fmt}")
        self.tp_label.setText(f"止盈价:  {tp_fmt}")

        lev = d.get("leverage", 0)
        pct = d.get("position_pct", 0)
        self.lev_label.setText(f"建议杠杆: {lev}x")
        self.pos_label.setText(f"仓位: {pct:.1f}%  (${d.get('notional_value', 0):,.2f})")

        change = d.get("change_24h", 0)
        h24 = d.get("high_24h", 0)
        l24 = d.get("low_24h", 0)
        chg_color = "#EF9A9A" if change < 0 else "#A5D6A7"
        self.price_label.setText(f"24h: <span style='color:{chg_color};'>{change:+.2f}%</span>  |  高 ${h24:,.2f}  低 ${l24:,.2f}")
        self.price_label.setTextFormat(Qt.TextFormat.RichText)


# ═══════════════════════════════════════════════════════════════════════
#  详细分析文本区
# ═══════════════════════════════════════════════════════════════════════

class DetailPanel(QTextEdit):
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

        lp = d.get("long_prob", 50)
        sp = d.get("short_prob", 50)
        long_bars = int(lp / 5)
        short_bars = int(sp / 5)
        parts.append("📊 多空概率")
        parts.append(f"   🟢 做多: {lp:.1f}%  {'■' * long_bars}")
        parts.append(f"   🔴 做空: {sp:.1f}%  {'■' * short_bars}")
        parts.append("")

        parts.append("📈 技术指标")
        parts.append(f"   RSI(14): {d.get('rsi', '—')}  |  ATR: {d.get('atr_pct', 0):.2f}%  |  MACD: {d.get('macd_trend', '—')}")
        parts.append(f"   结构: {d.get('structure', '—')}  |  量比: x{d.get('volume_ratio', 0):.1f}  |  资金费率: {d.get('funding_rate', 0):+.6f}")
        parts.append(f"   共振: {'✅ 多TF一致' if d.get('tf_aligned') else '❌ TF分化'}")
        parts.append("")

        reasons_l = d.get("reasons_long", [])
        if reasons_l:
            parts.append(f"🟢 做多理由 ({d.get('long_score', 0)}分)")
            for r in reasons_l:
                parts.append(f"   ✓ {r}")
            parts.append("")

        reasons_s = d.get("reasons_short", [])
        if reasons_s:
            parts.append(f"🔴 做空理由 ({d.get('short_score', 0)}分)")
            for r in reasons_s:
                parts.append(f"   ✓ {r}")
            parts.append("")

        warnings = d.get("warnings", [])
        if warnings:
            parts.append("⚠️ 风险提示")
            for w in warnings:
                parts.append(f"   · {w}")
            parts.append("")

        rr = d.get("rr_ratio")
        if rr:
            parts.append(f"📐 盈亏比: 1:{rr}")

        self.setText("\n".join(parts))
        self.verticalScrollBar().setValue(0)


# ═══════════════════════════════════════════════════════════════════════
#  分析页面（Tab 1）
# ═══════════════════════════════════════════════════════════════════════

class AnalysisPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(8, 8, 8, 8)

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

        self.card = ResultCard()
        layout.addWidget(self.card)

        self.detail = DetailPanel()
        layout.addWidget(self.detail, stretch=1)

        self.status_label = QLabel("就绪 ✅")
        self.status_label.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(self.status_label)

        self.setLayout(layout)

        self.analyze_btn.clicked.connect(self.run_analysis)

    def _quick_coin(self, coin: str):
        self.coin_input.setText(coin)
        self.run_analysis()

    def analyze_coin(self, coin: str):
        """外部调用（从涨幅/跌幅榜跳转）"""
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

        self._worker = AnalysisWorker(coin)
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


# ═══════════════════════════════════════════════════════════════════════
#  涨幅/跌幅榜单个币条目
# ═══════════════════════════════════════════════════════════════════════

class CoinItem(QFrame):
    """单个币的行条目"""
    clicked = Signal(str)  # 发送币种名称

    def __init__(self, rank: int, symbol: str, price: float,
                 change_pct: float, volume: float, is_gainer: bool):
        super().__init__()
        self.setObjectName("coin_item")
        self.setStyleSheet(COIN_ITEM_STYLE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(10, 6, 10, 6)

        # 排名
        rank_label = QLabel(f"#{rank}")
        rank_label.setFixedWidth(32)
        rank_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #78909C;")
        layout.addWidget(rank_label)

        # 币种名
        coin_name = symbol.replace("USDT", "")
        name_label = QLabel(coin_name)
        name_label.setFixedWidth(85)
        name_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #E3F2FD;")
        layout.addWidget(name_label)

        # 价格
        price_str = f"${price:,.6f}" if price < 1 else f"${price:,.2f}" if price < 10000 else f"${price:,.0f}"
        price_label = QLabel(price_str)
        price_label.setFixedWidth(110)
        price_label.setStyleSheet("font-size: 13px; color: #B0BEC5;")
        layout.addWidget(price_label)

        # 涨跌幅
        chg_color = "#A5D6A7" if change_pct > 0 else "#EF9A9A"
        chg_label = QLabel(f"{change_pct:+.2f}%")
        chg_label.setFixedWidth(80)
        chg_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        chg_label.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {chg_color};")
        layout.addWidget(chg_label)

        # 成交量
        vol_label = QLabel(f"${volume:.1f}M")
        vol_label.setFixedWidth(75)
        vol_label.setStyleSheet("font-size: 11px; color: #546E7A;")
        layout.addWidget(vol_label)

        layout.addStretch()

        # 分析按钮
        analyze_btn = QPushButton("分析")
        analyze_btn.setStyleSheet(SMALL_BTN)
        analyze_btn.clicked.connect(lambda: self.clicked.emit(coin_name))
        layout.addWidget(analyze_btn)

        self.setLayout(layout)

        # 点击条目本身也触发
        self.mouseReleaseEvent = lambda e: self.clicked.emit(coin_name)


# ═══════════════════════════════════════════════════════════════════════
#  涨幅榜页面（Tab 2）
# ═══════════════════════════════════════════════════════════════════════

class GainersPage(QWidget):
    def __init__(self, on_analyze=None):
        super().__init__()
        self.on_analyze = on_analyze
        self._worker = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.setContentsMargins(8, 8, 8, 8)

        top_layout = QHBoxLayout()
        title = QLabel("📈 涨幅榜 TOP 20")
        title.setStyleSheet("font-size: 18px; color: #A5D6A7; font-weight: bold;")
        top_layout.addWidget(title)

        top_layout.addStretch()

        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.setStyleSheet(REFRESH_BTN)
        self.refresh_btn.clicked.connect(self.refresh)
        self.refresh_btn.setFixedHeight(30)
        top_layout.addWidget(self.refresh_btn)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #78909C; font-size: 11px;")
        top_layout.addWidget(self.status_label)

        layout.addLayout(top_layout)

        # 滚动区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setSpacing(4)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.addStretch()

        scroll.setWidget(self.list_widget)
        layout.addWidget(scroll, stretch=1)

        # 加载提示
        self.hint_label = QLabel("点击「刷新」加载涨幅榜...")
        self.hint_label.setStyleSheet("color: #546E7A; font-size: 14px;")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list_layout.insertWidget(0, self.hint_label)

        self.setLayout(layout)

    def refresh(self):
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("⏳ 加载中...")
        self.status_label.setText("正在获取数据...")

        # 移除旧条目（保留hint）
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

        gainers = data.get("gainers", [])
        if not gainers:
            self.status_label.setText("暂无数据")
            self.hint_label.show()
            return

        self.hint_label.hide()

        for i, coin in enumerate(gainers):
            item = CoinItem(
                rank=i + 1,
                symbol=coin["symbol"],
                price=coin["price"],
                change_pct=coin["change_pct"],
                volume=coin["volume"],
                is_gainer=True,
            )
            item.clicked.connect(self._on_coin_click)
            # 插入到 stretch 前面
            self.list_layout.insertWidget(self.list_layout.count() - 1, item)

        self.status_label.setText(f"✅ {len(gainers)} 个币种  ({datetime.now():%H:%M})")

    def _on_coin_click(self, coin: str):
        if self.on_analyze:
            self.on_analyze(coin)


# ═══════════════════════════════════════════════════════════════════════
#  跌幅榜页面（Tab 3）
# ═══════════════════════════════════════════════════════════════════════

class LosersPage(QWidget):
    def __init__(self, on_analyze=None):
        super().__init__()
        self.on_analyze = on_analyze
        self._worker = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.setContentsMargins(8, 8, 8, 8)

        top_layout = QHBoxLayout()
        title = QLabel("📉 跌幅榜 TOP 20")
        title.setStyleSheet("font-size: 18px; color: #EF9A9A; font-weight: bold;")
        top_layout.addWidget(title)

        top_layout.addStretch()

        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.setStyleSheet(REFRESH_BTN)
        self.refresh_btn.clicked.connect(self.refresh)
        self.refresh_btn.setFixedHeight(30)
        top_layout.addWidget(self.refresh_btn)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #78909C; font-size: 11px;")
        top_layout.addWidget(self.status_label)

        layout.addLayout(top_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setSpacing(4)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.addStretch()

        scroll.setWidget(self.list_widget)
        layout.addWidget(scroll, stretch=1)

        self.hint_label = QLabel("点击「刷新」加载跌幅榜...")
        self.hint_label.setStyleSheet("color: #546E7A; font-size: 14px;")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list_layout.insertWidget(0, self.hint_label)

        self.setLayout(layout)

    def refresh(self):
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("⏳ 加载中...")
        self.status_label.setText("正在获取数据...")

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

        losers = data.get("losers", [])
        if not losers:
            self.status_label.setText("暂无数据")
            self.hint_label.show()
            return

        self.hint_label.hide()

        for i, coin in enumerate(losers):
            item = CoinItem(
                rank=i + 1,
                symbol=coin["symbol"],
                price=coin["price"],
                change_pct=coin["change_pct"],
                volume=coin["volume"],
                is_gainer=False,
            )
            item.clicked.connect(self._on_coin_click)
            self.list_layout.insertWidget(self.list_layout.count() - 1, item)

        self.status_label.setText(f"✅ {len(losers)} 个币种  ({datetime.now():%H:%M})")

    def _on_coin_click(self, coin: str):
        if self.on_analyze:
            self.on_analyze(coin)


# ═══════════════════════════════════════════════════════════════════════
#  主窗口
# ═══════════════════════════════════════════════════════════════════════

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CryptoAI — 币安信号雷达")
        self.resize(750, 800)
        self.setMinimumSize(520, 600)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(8)

        title = QLabel("🦐 币安超短线信号雷达")
        title_font = QFont()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(TAB_STYLE)

        # 分析页
        self.analysis_page = AnalysisPage()
        self.tabs.addTab(self.analysis_page, "🔍 分析")

        # 涨幅榜（点击跳转到分析页）
        self.gainers_page = GainersPage(on_analyze=self._switch_to_analyze)
        self.tabs.addTab(self.gainers_page, "📈 涨幅榜")

        # 跌幅榜
        self.losers_page = LosersPage(on_analyze=self._switch_to_analyze)
        self.tabs.addTab(self.losers_page, "📉 跌幅榜")

        layout.addWidget(self.tabs, stretch=1)
        self.setLayout(layout)

    def _switch_to_analyze(self, coin: str):
        """从涨幅/跌幅榜跳转到分析页"""
        self.tabs.setCurrentIndex(0)
        self.analysis_page.analyze_coin(coin)


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
        QScrollBar:vertical {
            background: #0f3460;
            width: 8px;
            border-radius: 4px;
        }
        QScrollBar::handle:vertical {
            background: #1a5276;
            border-radius: 4px;
            min-height: 30px;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0px;
        }
    """)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
