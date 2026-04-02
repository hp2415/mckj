from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, 
    QPushButton, QListWidget, QListWidgetItem, QLabel, QFrame
)
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtCore import Qt, Signal, QSize
import httpx
import io

class ProductItemWidget(QFrame):
    """
    单个商品列表项的自定义 UI 容器。
    包含：封面图、商品名、价格、商品直链。
    """
    def __init__(self, product_data, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedHeight(120)
        self.setStyleSheet("""
            QFrame {
                background-color: #ffffff;
                border: 1px solid #eeeeee;
                border-radius: 8px;
            }
            QLabel {
                border: none;
            }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(15)

        # 1. 封面图占位 (左侧)
        self.img_label = QLabel()
        self.img_label.setFixedSize(100, 100)
        self.img_label.setStyleSheet("background-color: #f8f8f8; border-radius: 4px;")
        self.img_label.setText("加载中...")
        self.img_label.setScaledContents(True)
        layout.addWidget(self.img_label)

        # 2. 信息面板 (右侧)
        info_layout = QVBoxLayout()
        info_layout.setSpacing(5)

        self.name_label = QLabel(product_data.get("product_name", "未知商品"))
        self.name_label.setWordWrap(True)
        self.name_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #333;")
        info_layout.addWidget(self.name_label)

        self.price_label = QLabel(f"￥ {product_data.get('price', 0.0)}")
        self.price_label.setStyleSheet("font-size: 16px; color: #ff4d4f; font-weight: bold;")
        info_layout.addWidget(self.price_label)

        self.supplier_label = QLabel(f"供货商: {product_data.get('supplier_name', '未知')}")
        self.supplier_label.setStyleSheet("font-size: 11px; color: #888;")
        info_layout.addWidget(self.supplier_label)
        
        info_layout.addStretch()
        layout.addLayout(info_layout)

    def update_image(self, pixmap):
        """由外部线程加载成功后主线程调用更新"""
        self.img_label.setPixmap(pixmap)

class MainWindow(QMainWindow):
    """
    基于 PySide6 的桌面端主窗口。
    """
    search_requested = Signal(str, int, int) # 发送信号请求搜索

    def __init__(self, username: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"微企 AI 助手 - [当前登录: {username}]")
        self.resize(800, 600)
        self.setStyleSheet("background-color: #fafafa;")

        # 中心布局
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        # 1. 头部搜索区
        header_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("请输入关键词搜索 832 货源...")
        self.search_input.setFixedHeight(40)
        self.search_input.setStyleSheet("padding: 0 10px; border: 1px solid #ddd; border-radius: 4px; background: white;")
        
        self.search_btn = QPushButton("搜索货源")
        self.search_btn.setFixedHeight(40)
        self.search_btn.setFixedWidth(100)
        self.search_btn.setCursor(Qt.PointingHandCursor)
        self.search_btn.clicked.connect(self._on_search_clicked)
        
        header_layout.addWidget(self.search_input)
        header_layout.addWidget(self.search_btn)
        main_layout.addLayout(header_layout)

        # 2. 商品列表区
        self.product_list = QListWidget()
        self.product_list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.product_list.setSelectionMode(QListWidget.NoSelection)
        self.product_list.setStyleSheet("border: none; background: transparent;")
        main_layout.addWidget(self.product_list)

        # 3. 底部状态与操作区
        self.load_more_btn = QPushButton("⏬ 点击加载更多数据...")
        self.load_more_btn.setFixedHeight(40)
        self.load_more_btn.hide() # 初始隐藏
        self.load_more_btn.clicked.connect(self._on_load_more_clicked)
        main_layout.addWidget(self.load_more_btn)

        self.current_skip = 0
        self.current_limit = 20

    def _on_search_clicked(self):
        self.current_skip = 0
        self.product_list.clear() # 切换搜索词，清空列表
        self.search_requested.emit(self.search_input.text().strip(), self.current_skip, self.current_limit)

    def _on_load_more_clicked(self):
        self.current_skip += self.current_limit
        self.search_requested.emit(self.search_input.text().strip(), self.current_skip, self.current_limit)

    def add_product_card(self, product_data):
        """将一条商品数据转化为 UI 卡片并添加至列表"""
        item = QListWidgetItem(self.product_list)
        item.setSizeHint(QSize(0, 130)) # 设置项的高度
        
        widget = ProductItemWidget(product_data)
        self.product_list.setItemWidget(item, widget)
        return widget

    def update_has_more(self, has_more: bool):
        """控制加载按钮的显隐"""
        self.load_more_btn.setVisible(has_more)
