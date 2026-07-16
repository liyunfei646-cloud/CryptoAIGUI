"""
CryptoAIGUI.py — 币安信号雷达桌面版 主入口
===========================================
启动器：导入前端组件并启动 PySide6 应用。
所有 UI 定义见 ui.py，分析引擎见 analyzer.py。
"""

import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
from ui import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    font = QFont("Inter, 'Segoe UI', system-ui, sans-serif", 10)
    app.setFont(font)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
