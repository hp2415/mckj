from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel, QSizePolicy
from PySide6.QtCore import Qt, QSize
from qfluentwidgets import (
    BodyLabel, CaptionLabel, StrongBodyLabel, TransparentToolButton, FluentIcon,
    isDarkTheme
)

class OrderCardWidget(QFrame):
    """
    客户历史订单卡片：展示更丰富的商业数据，采用 Fluent Design 风格。
    """
    def __init__(self, order_data, parent=None):
        super().__init__(parent)
        self.order_data = order_data
        self.setObjectName("OrderCard")
        # 主布局：纵向
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(15, 12, 12, 12) # 增加左侧边距以配合状态条
        self.main_layout.setSpacing(8)

        # 1. 顶层：订单编号 + 状态
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 5, 0) # 增加右侧预留空间，防止状态标签贴边
        header_layout.setSpacing(10)
        
        self.dddh_lbl = CaptionLabel(f"单号: {order_data.get('dddh', '-')}")
        self.dddh_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        
        self.status_lbl = StrongBodyLabel(order_data.get('status_name', '未知状态'))
        self.status_lbl.setObjectName("OrderStatusLabel")
            
        header_layout.addWidget(self.dddh_lbl)
        header_layout.addStretch()
        header_layout.addWidget(self.status_lbl)
        self.main_layout.addLayout(header_layout)

        # 2. 中间层：商品名称 (加粗)
        self.title_lbl = BodyLabel(order_data.get('product_title', '未指定商品'))
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.main_layout.addWidget(self.title_lbl)

        # 3. 信息层：店铺、支付方式
        info_layout = QHBoxLayout()
        self.store_lbl = CaptionLabel(f"店铺: {order_data.get('store', '-')}")
        self.store_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.pay_type_lbl = CaptionLabel(f"支付方式: {order_data.get('pay_type_name', '-')}")
        self.pay_type_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info_layout.addWidget(self.store_lbl)
        info_layout.addSpacing(20)
        info_layout.addWidget(self.pay_type_lbl)
        info_layout.addStretch()
        self.main_layout.addLayout(info_layout)

        # 4. 金额层
        price_layout = QHBoxLayout()
        self.amount_lbl = QLabel()
        self.amount_lbl.setTextFormat(Qt.RichText)
        self.amount_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        price_layout.addWidget(self.amount_lbl)
        price_layout.addStretch()
        self.main_layout.addLayout(price_layout)

        # 5. 物流层：收货人 + 地址
        self.address_lbl = CaptionLabel(f"收货信息: {order_data.get('consignee', '-')} | {order_data.get('consignee_address', '-')}")
        self.address_lbl.setWordWrap(True)
        self.address_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.main_layout.addWidget(self.address_lbl)

        # 6. 底部：时间 + 备注
        footer_layout = QHBoxLayout()
        self.time_lbl = CaptionLabel(order_data.get('order_time', '-'))
        footer_layout.addWidget(self.time_lbl)
        
        remark = order_data.get('remark', '')
        if remark:
            self.remark_lbl = CaptionLabel(f"备注: {remark}")
            self.remark_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            # 备注颜色适配：橘黄色
            self.remark_lbl.setStyleSheet("color: #fa8c16; font-style: italic;")
            footer_layout.addStretch()
            footer_layout.addWidget(self.remark_lbl)
            
        self.main_layout.addLayout(footer_layout)

        # 7. 应用动态主题样式
        self._apply_theme_styles()

    def _apply_theme_styles(self):
        """应用动态主题色，特别是针对状态标签的语义化底色"""
        is_dark = isDarkTheme()
        
        # 1. 更新容器边框和背景 (QFrame)
        bg = "rgba(255, 255, 255, 0.05)" if is_dark else "rgba(0, 0, 0, 0.02)"
        border = "rgba(255, 255, 255, 0.1)" if is_dark else "rgba(0, 0, 0, 0.08)"
        self.setStyleSheet(f"""
            QFrame#OrderCard {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
        """)


        # 3. 更新状态标签配色
        
        # 2. 核心状态检测与视觉增强
        status_text = self.status_lbl.text()
        # 语义化颜色定义
        if "已完成" in status_text or "成功" in status_text or "已支付" in status_text:
            status_color = "#52c41a" # 绿色：正向
            status_bg = "#f6ffed"
            pay_prefix = "实付额"
            side_color = "#52c41a"
            if is_dark:
                status_color, status_bg = "#73d13d", "#162312"
        elif "待" in status_text or "进行中" in status_text or "处理中" in status_text:
            status_color = "#faad14" # 黄色：中性/等待
            status_bg = "#fffbe6"
            pay_prefix = "待支付"
            side_color = "#faad14"
            if is_dark:
                status_color, status_bg = "#ffc53d", "#2b2111"
        elif "退" in status_text or "取消" in status_text or "关闭" in status_text:
            status_color = "#ff4d4f" # 红色：负向/终止
            status_bg = "#fff1f0"
            pay_prefix = "订单额"
            side_color = "#ff4d4f"
            if is_dark:
                status_color, status_bg = "#ff7875", "#2a1215"
        else:
            status_color = "#8c8c8c" # 灰色：未知/常规
            status_bg = "#f5f5f5"
            pay_prefix = "订单额"
            side_color = "#bfbfbf"
            if is_dark:
                status_color, status_bg = "#bfbfbf", "#1f1f1f"

        # 应用卡片样式：新增强大的左侧状态指示条
        card_bg = "#2d2d2d" if is_dark else "#ffffff"
        border_col = "#404040" if is_dark else "#e8e8e8"
        self.setStyleSheet(f"""
            QFrame#OrderCard {{
                background-color: {card_bg};
                border: 1px solid {border_col};
                border-left: 5px solid {side_color};
                border-radius: 8px;
            }}
        """)

        self.status_lbl.setStyleSheet(f"""
            QLabel {{
                color: {status_color};
                background-color: {status_bg};
                border: 1px solid {status_color}44;
                padding: 2px 8px;
                border-radius: 4px;
                font-size: 11px;
                font-weight: bold;
            }}
        """)

        # 3. 更新富文本金额颜色与前缀
        pay_amt = self.order_data.get('pay_amount', 0.0)
        freight = self.order_data.get('freight', 0.0)
        amt_color = "#ff4d4f" if is_dark else "#cf1322"
        prefix_color = "#dfdfdf" if is_dark else "#555555"
        
        amt_text = f"<span style='color: {prefix_color}; font-size: 12px;'>{pay_prefix}: </span>"
        amt_text += f"<span style='color: {amt_color}; font-size: 18px; font-weight: 900;'>¥{pay_amt}</span>"
        if freight > 0:
            amt_text += f" <span style='color: #888888; font-size: 11px;'>(含运费 ¥{freight})</span>"
        else:
            amt_text += f" <span style='color: #888888; font-size: 11px;'>(免运费)</span>"
        self.amount_lbl.setText(amt_text)

        # 3. 文字颜色微调
        if is_dark:
            self.title_lbl.setStyleSheet("font-weight: bold; font-size: 14px; color: #dfdfdf;")
            self.dddh_lbl.setStyleSheet("color: #aaaaaa;")
            self.address_lbl.setStyleSheet("color: #aaaaaa;")
            self.time_lbl.setStyleSheet("color: #888888;")
        else:
            self.title_lbl.setStyleSheet("font-weight: bold; font-size: 14px; color: #1a1a1a;")
            self.dddh_lbl.setStyleSheet("color: #666666;")
            self.address_lbl.setStyleSheet("color: #555555;")
            self.time_lbl.setStyleSheet("color: #999999;")
