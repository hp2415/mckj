"""
商品卡片组件：ProductItemWidget
对应 UI_implementation.md Phase 5 — 商品页改造
"""
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QVBoxLayout, QLabel, QApplication,
    QGraphicsDropShadowEffect,
)
from PySide6.QtCore import Qt, Signal, QSize, QTimer
from PySide6.QtGui import QColor


class ProductItemWidget(QFrame):
    """
    单个商品卡片组件：注入物理投影质感与弹性化长标题支持。
    """
    full_copy_requested = Signal(str)  # 请求高清原图复制

    def __init__(self, product_data, parent=None):
        super().__init__(parent)
        self.product_data = product_data
        self.setObjectName("ProductCard")
        # 释放高度限制，允许长标题无限换行撑开
        self.setMinimumHeight(120)

        # 注入物理阴影特效
        self.shadow = QGraphicsDropShadowEffect(self)
        self.shadow.setBlurRadius(12)
        self.shadow.setXOffset(0)
        self.shadow.setYOffset(2)
        self.shadow.setColor(QColor(0, 0, 0, 20))
        self.setGraphicsEffect(self.shadow)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(15)

        # 1. 商品图片
        self.img_label = QLabel()
        self.img_label.setObjectName("ProductImage")
        self.img_label.setFixedSize(110, 120)  # 放大尺寸提升视觉直观度
        self.img_label.setScaledContents(True)
        self.img_label.mousePressEvent = self._on_image_clicked
        layout.addWidget(self.img_label, 0, Qt.AlignTop | Qt.AlignHCenter)

        # 2. 信息栏 (使用自适应权重分配)
        self.info_container = QFrame()
        info_layout = QVBoxLayout(self.info_container)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(4)

        # 商品全名 (开启换行)
        p_name = product_data.get("product_name", "未知商品")
        self.name_label = QLabel(p_name)
        self.name_label.setObjectName("ProductName")
        self.name_label.setWordWrap(True)
        self.name_label.setToolTip(p_name)
        self.name_label.mousePressEvent = self._on_name_clicked
        info_layout.addWidget(self.name_label)

        # 价格行
        price = product_data.get('price', 0.0)
        unit = product_data.get('unit', '')
        price_text = f"￥ {price}"
        if unit:
            price_text += f"/{unit}"

        self.price_label = QLabel(price_text)
        self.price_label.setObjectName("ProductPrice")
        info_layout.addWidget(self.price_label)

        # 供应商名 (开启辅助全称显示)
        supplier = product_data.get('supplier_name', '平台自营')
        self.supplier_label = QLabel(f"供货: {supplier}")
        self.supplier_label.setObjectName("ProductSupplier")
        self.supplier_label.setWordWrap(True)
        self.supplier_label.setToolTip(f"{supplier}")
        info_layout.addWidget(self.supplier_label)

        layout.addWidget(self.info_container, 1)  # 信息区占据剩余宽度

    def sizeHint(self):
        """核心：确保向 QListWidget 报备正确的动态高度"""
        sh = super().sizeHint()
        # 根据内容自动计算推荐高度，但不低于 120 也不高于 220
        h = max(120, sh.height() + 20)
        return QSize(sh.width(), min(h, 220))

    def _on_image_clicked(self, event):
        """点击图片：请求从 L2 缓存中提取原始图像进行复制"""
        url = self.product_data.get("cover_img")
        if url:
            self.full_copy_requested.emit(url)
            # 视觉变色反馈
            self.setStyleSheet("background-color: #e6f7ff; border: 1px solid #1890ff;")
            # 反馈结束后清空 inline 样式，使 QSS 重新接管状态（恢复 hover 等效果）
            QTimer.singleShot(200, lambda: self.setStyleSheet(""))

    def _on_name_clicked(self, event):
        """点击名称：复制『名称+链接』至剪贴板"""
        name = self.product_data.get("product_name", "")
        url = self.product_data.get("product_url", "暂无外部链接")
        text = f"{name}\n{url}"
        QApplication.clipboard().setText(text)
        self.setStyleSheet("background-color: #f6ffed; border: 1px solid #52c41a;")
        # 同样清空 inline 样式以恢复正常 QSS 状态
        QTimer.singleShot(200, lambda: self.setStyleSheet(""))

    def update_image(self, pixmap):
        self.img_label.setPixmap(pixmap)
