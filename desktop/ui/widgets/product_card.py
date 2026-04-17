"""
商品卡片组件：ProductItemWidget
对应 UI_implementation.md Phase 5 — 商品页改造
"""
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QVBoxLayout, QLabel, QApplication,
    QGraphicsDropShadowEffect,
)
from PySide6.QtCore import Qt, Signal, QSize, QTimer, QSettings
from PySide6.QtGui import QColor
from qfluentwidgets import isDarkTheme, setTheme, Theme, setThemeColor, ToolTipFilter, ToolTipPosition


class ProductItemWidget(QFrame):
    """
    单个商品卡片组件：注入物理投影质感与弹性化长标题支持。
    """
    full_copy_requested = Signal(str)  # 请求高清原图复制

    def __init__(self, product_data, parent=None):
        super().__init__(parent)
        self.product_data = product_data
        self.setObjectName("ProductCard")
        # 释放高度限制
        self.setMinimumHeight(120)

        # 1. 商品图片
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
        info_layout.setSpacing(0)

        # 商品全名 (开启换行)
        p_name = product_data.get("product_name", "未知商品")
        self.name_label = QLabel(p_name)
        self.name_label.setObjectName("ProductName")
        self.name_label.setWordWrap(True)
        self.name_label.setToolTip(p_name)
        self.name_label.installEventFilter(ToolTipFilter(self.name_label, showDelay=300, position=ToolTipPosition.TOP))
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
        self.supplier_label.setToolTip(supplier)
        self.supplier_label.installEventFilter(ToolTipFilter(self.supplier_label, showDelay=300, position=ToolTipPosition.TOP))
        info_layout.addWidget(self.supplier_label)

        # 种类标签 (微型面包屑)
        c1 = product_data.get("cat1")
        c2 = product_data.get("cat2")
        c3 = product_data.get("cat3")
        if c1:
            cat_text = f"分类: {c1}"
            if c2: cat_text += f" > {c2}"
            if c3: cat_text += f" > {c3}"
            self.cat_label = QLabel(cat_text)
            self.cat_label.setObjectName("ProductCat")
            self.cat_label.setToolTip(cat_text)
            self.cat_label.installEventFilter(ToolTipFilter(self.cat_label, showDelay=300, position=ToolTipPosition.TOP))
            info_layout.addWidget(self.cat_label)

        # 产地标签
        prov = product_data.get("province")
        city = product_data.get("city")
        dist = product_data.get("district")
        if prov:
            org_text = f"产地: {prov}"
            if city: org_text += f"-{city}"
            if dist: org_text += f"-{dist}"
            self.org_label = QLabel(org_text)
            self.org_label.setObjectName("ProductOriginTag")
            info_layout.addWidget(self.org_label)

        layout.addWidget(self.info_container, 1)  # 信息区占据剩余宽度
        
        # 应用初始样式
        self._apply_theme_style()

    def sizeHint(self):
        """核心：确保向 QListWidget 报备正确的动态高度"""
        sh = super().sizeHint()
        # 根据内容自动计算推荐高度，但不低于 120 也不高于 220
        h = max(120, sh.height() + 20)
        return QSize(sh.width(), min(h, 220))

    def _apply_theme_style(self):
        """动态适配深浅主题样式"""
        is_dark = isDarkTheme()
        self._reset_card_style()  # 确保背景色、边框等静态属性初始就位
        
        # 1. 更新阴影
        if not hasattr(self, "shadow"):
            self.shadow = QGraphicsDropShadowEffect(self)
            self.shadow.setBlurRadius(12)
            self.shadow.setXOffset(0)
            self.shadow.setYOffset(2)
            self.setGraphicsEffect(self.shadow)
        
        shadow_col = QColor(0, 0, 0, 80) if is_dark else QColor(0, 0, 0, 20)
        self.shadow.setColor(shadow_col)
        
        # 2. 文字色彩适配
        title_col = "#eeeeee" if is_dark else "#1a1a1a"
        self.name_label.setStyleSheet(f"font-weight: bold; font-size: 13px; margin-bottom: 2px; color: {title_col};")
        
        price_col = "#ff6b6b" if is_dark else "#ff4d4f"
        self.price_label.setStyleSheet(f"font-weight: bold; font-size: 14px; color: {price_col}; margin-bottom: 2px;")
        
        detail_col = "#aaaaaa" if is_dark else "#777777"
        sub_style = f"font-size: 11px; color: {detail_col}; padding: 0px; margin: 0px;"
        self.supplier_label.setStyleSheet(sub_style)
        if hasattr(self, "cat_label"):
            self.cat_label.setStyleSheet(sub_style)
        if hasattr(self, "org_label"):
            self.org_label.setStyleSheet(sub_style)

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
        feedback_bg = "#21331d" if isDarkTheme() else "#f6ffed"
        self.setStyleSheet(f"background-color: {feedback_bg}; border: 1px solid #52c41a;")
        # 同样清空 inline 样式以恢复正常状态
        QTimer.singleShot(200, self._reset_card_style)

    def _reset_card_style(self):
        """重置卡片样式到主题默认态"""
        is_dark = isDarkTheme()
        bg = "#2c2c2c" if is_dark else "#ffffff"
        # 移除强制的 border-bottom，改用统一细边框以确保交互反馈（border）能完美覆盖
        border_col = "#3f3f3f" if is_dark else "#e5e5e5"
        self.setStyleSheet(f"""
            QFrame#ProductCard {{ 
                background-color: {bg}; 
                border: 1px solid {border_col};
                border-radius: 8px;
            }}
        """)

    def update_image(self, pixmap):
        self.img_label.setPixmap(pixmap)
