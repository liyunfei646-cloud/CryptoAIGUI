"""
CryptoAIGUI.py — 币安信号雷达桌面版
===================================
基于 PySide6 的桌面应用，调用 analyzer 模块分析币种。

用法:
  pip install PySide6
  python CryptoAIGUI.py
"""

import sys
from datetime import datetime, timezone, timedelta

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QTextEdit, QLabel, QStatusBar
)
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QFont

from analyzer import analyze_coin


class Worker(QThread):
    """后台线程，避免分析时界面卡死"""
    finished = Signal(str)  # 结果文本
    error = Signal(str)     # 错误消息

    def __init__(self, symbol: str, balance: float = 1000.0):
        super().__init__()
        self.symbol = symbol
        self.balance = balance

    def run(self):
        try:
            result = analyze_coin(self.symbol, self.balance)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(f"❌ 分析异常: {e}")


class MainWindow(QWidget):
    """主窗口"""

    def __init__(self):
        super().__init__()

        self.setWindowTitle("CryptoAI — 币安信号雷达")
        self.resize(720, 680)
        self.setMinimumSize(520, 480)

        self._setup_ui()

        # 当前分析线程
        self._worker: Worker | None = None

    def _setup_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(12)

        # ── 标题 ──
        title = QLabel("🦐 币安超短线信号雷达")
        title_font = QFont()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # ── 输入行 ──
        input_layout = QHBoxLayout()

        self.coin_input = QLineEdit()
        self.coin_input.setPlaceholderText("请输入币种，例如 BTC / ETH / SOL / OPG")
        self.coin_input.setMinimumHeight(36)

        self.analyze_btn = QPushButton("🔍 开始分析")
        self.analyze_btn.setMinimumHeight(36)
        self.analyze_btn.setMinimumWidth(120)
        self.analyze_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
            QPushButton:disabled {
                background-color: #90CAF9;
            }
        """)

        input_layout.addWidget(self.coin_input, stretch=3)
        input_layout.addWidget(self.analyze_btn, stretch=1)
        layout.addLayout(input_layout)

        # ── 常用币种快捷按钮 ──
        quick_layout = QHBoxLayout()
        quick_layout.setSpacing(6)
        for coin in ["BTC", "ETH", "SOL", "OPG", "DOGE", "BNB"]:
            btn = QPushButton(coin)
            btn.setFixedHeight(28)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #37474F;
                    color: #B0BEC5;
                    border: 1px solid #546E7A;
                    border-radius: 4px;
                    font-size: 12px;
                }
                QPushButton:hover {
                    background-color: #546E7A;
                    color: white;
                }
            """)
            btn.clicked.connect(lambda checked, c=coin: self._quick_coin(c))
            quick_layout.addWidget(btn)
        quick_layout.addStretch()
        layout.addLayout(quick_layout)

        # ── 结果显示区 ──
        self.result_area = QTextEdit()
        self.result_area.setReadOnly(True)
        self.result_area.setFont(QFont("Consolas, Courier New, monospace", 11))
        self.result_area.setStyleSheet("""
            QTextEdit {
                background-color: #1a1a2e;
                color: #e0e0e0;
                border: 1px solid #333;
                border-radius: 6px;
                padding: 10px;
            }
        """)
        self.result_area.setPlaceholderText(
            "输入币种后点击「开始分析」...\n\n"
            "示例: BTC / ETHUSDT / OPG"
        )
        layout.addWidget(self.result_area, stretch=1)

        # ── 状态栏 ──
        self.status_label = QLabel("就绪 ✅")
        self.status_label.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(self.status_label)

        self.setLayout(layout)

        # ── 信号绑定 ──
        self.analyze_btn.clicked.connect(self.run_analysis)
        self.coin_input.returnPressed.connect(self.run_analysis)

    def _quick_coin(self, coin: str):
        """点击快捷按钮"""
        self.coin_input.setText(coin)
        self.run_analysis()

    def run_analysis(self):
        coin = self.coin_input.text().strip()
        if not coin:
            self.result_area.setText("⚠️ 请输入币种名称")
            return

        # 禁用按钮，防止重复点击
        self.analyze_btn.setEnabled(False)
        self.analyze_btn.setText("⏳ 分析中...")
        self.result_area.setText(f"🔄 正在分析 {coin.upper()}，请稍候...")
        self.status_label.setText(f"⏳ 正在获取 {coin.upper()} 数据...")

        # 后台线程执行
        self._worker = Worker(coin)
        self._worker.finished.connect(self._on_result)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_result(self, result: str):
        self.result_area.setText(result)
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("🔍 开始分析")
        self.status_label.setText(f"✅ 完成 ({datetime.now():%H:%M:%S})")

    def _on_error(self, msg: str):
        self.result_area.setText(msg)
        self.analyze_btn.setEnabled(True)
        self.analyze_btn.setText("🔍 开始分析")
        self.status_label.setText("❌ 分析失败")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 整体深色风格
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
