"""
客户全息档案面板：CustomerInfoWidget
对应 UI_implementation.md Phase 3 — 客户信息表单改造
"""
import os
import json

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QFrame,
    QLabel, QLineEdit, QTextEdit, QPushButton,
)
from PySide6.QtCore import Qt, Signal, QDate
from logger_cfg import logger

from ui.widgets.form_controls import NoScrollComboBox, MultiSelectComboBox, DatePickerBtn
from ui.widgets.cascader import RegionCascader


class CustomerInfoWidget(QWidget):
    """
    客户详情信息面板：视觉风格大一统。
    """
    save_clicked = Signal(str, dict)
    history_clicked = Signal(int)  # 传递客户 ID

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 10, 15, 10)
        layout.setSpacing(10)

        header = QLabel("客户全息档案")
        header.setObjectName("InfoHeader")
        layout.addWidget(header)

        # 统一表单样式
        self.form_container = QFrame()
        self.form_container.setObjectName("FormContainer")
        form_layout = QFormLayout(self.form_container)
        form_layout.setLabelAlignment(Qt.AlignRight)
        form_layout.setVerticalSpacing(6)
        form_layout.setHorizontalSpacing(15)

        # 1. 核心只读
        self.edit_name = QLineEdit()
        self.edit_name.setReadOnly(True)
        self.edit_phone = QLineEdit()
        self.edit_phone.setReadOnly(True)

        # 2. 动态选项与组件
        self.combo_unit = NoScrollComboBox()
        self.combo_unit.setPlaceholderText("请选择所属单位...")
        self.combo_purchase_type = NoScrollComboBox()
        self.combo_purchase_type.setPlaceholderText("请选择采购模式...")
        self.edit_wechat_remark = QLineEdit()
        self.edit_wechat_remark.setPlaceholderText("填入客户的微信备注")

        # 加载本地城市数据
        self._pca_data = {}
        pca_path = os.path.join(os.path.dirname(__file__), "..", "pca.json")
        if os.path.exists(pca_path):
            with open(pca_path, "r", encoding="utf-8") as f:
                self._pca_data = json.load(f)

        # 网页级联选择器风格菜单
        self.combo_division = RegionCascader(self._pca_data)

        self.edit_contact_date = DatePickerBtn()

        self.combo_purchase_months = MultiSelectComboBox()

        self.btn_historical_amount = QPushButton("0.00 元")
        self.btn_historical_amount.setObjectName("HistoryAmountBtn")
        self.btn_historical_amount.setFlat(True)
        self.btn_historical_amount.clicked.connect(
            lambda: self.history_clicked.emit(self.current_customer_id) if self.current_customer_id else None
        )

        # 3. 业务主观字段
        self.edit_title = QLineEdit()
        self.edit_title.setPlaceholderText("例如：李局、张总")
        self.edit_budget = QLineEdit()
        self.edit_budget.setPlaceholderText("预计单笔采购预算")
        self.edit_profile = QTextEdit()
        self.edit_profile.setPlaceholderText("性格、偏好、历史沟通记录...")
        self.edit_profile.setMinimumHeight(100)

        form_layout.addRow("真实姓名:", self.edit_name)
        form_layout.addRow("联系电话:", self.edit_phone)
        form_layout.addRow("微信备注:", self.edit_wechat_remark)
        form_layout.addRow("所属单位:", self.combo_unit)
        form_layout.addRow("行政区划:", self.combo_division)
        form_layout.addRow("建联日期:", self.edit_contact_date)
        form_layout.addRow("采购类型:", self.combo_purchase_type)
        form_layout.addRow("采货月份:", self.combo_purchase_months)
        form_layout.addRow("历史总额:", self.btn_historical_amount)
        form_layout.addRow("当前称呼:", self.edit_title)
        form_layout.addRow("采购预算:", self.edit_budget)
        form_layout.addRow("私域画像:", self.edit_profile)

        layout.addWidget(self.form_container)

        self.save_btn = QPushButton("保存全部跟进信息")
        self.save_btn.setObjectName("SaveBtn")
        self.save_btn.setFixedHeight(36)
        self.save_btn.clicked.connect(self._on_save_clicked)
        layout.addWidget(self.save_btn)

        layout.addStretch()
        self.current_phone = None
        self.current_customer_id = None

    def populate_combo_boxes(self, configs_dict):
        """填充后台字典下发的数据 (原生占位符模式)"""
        self.combo_unit.clear()
        self.combo_unit.addItems(configs_dict.get("unit_type_choices", []))
        self.combo_unit.setCurrentIndex(-1)

        self.combo_purchase_type.clear()
        self.combo_purchase_type.addItems(configs_dict.get("purchase_type_choices", []))
        self.combo_purchase_type.setCurrentIndex(-1)

        months = [f"{i}月" for i in range(1, 13)]
        self.combo_purchase_months.model.clear()
        self.combo_purchase_months.addItemsChecked(months)
        self.combo_purchase_months.lineEdit().clear()

    def set_customer(self, data):
        self.current_phone = data.get("phone")
        self.current_customer_id = data.get("id")
        self.edit_name.setText(data.get("customer_name", "-"))
        self.edit_phone.setText(data.get("phone", "-"))

        # 下拉框赋值优化：原生负索引模式
        u_idx = self.combo_unit.findText(data.get("unit_type", ""))
        self.combo_unit.setCurrentIndex(u_idx)

        self.combo_division.setCurrentText(data.get("admin_division", "") or "")

        p_idx = self.combo_purchase_type.findText(data.get("purchase_type", ""))
        self.combo_purchase_type.setCurrentIndex(p_idx)

        # 多选框：根据数据长度自适应
        months_str = data.get("purchase_months", "") or ""
        months_list = [m.strip() for m in months_str.split(",") if m.strip()]
        self.combo_purchase_months.set_checked_items(months_list)
        if not months_list:
            self.combo_purchase_months.lineEdit().clear()

        contact_dt = data.get("contact_date")
        if contact_dt:
            try:
                year, month, day = map(int, contact_dt.split("-"))
                self.edit_contact_date.setDate(QDate(year, month, day))
            except Exception as e:
                logger.warning(f"客户建档日期解析失败: {e} ({contact_dt})")

        hist_amt = data.get("historical_amount", 0.0)
        hist_cnt = data.get("historical_order_count", 0)
        self.btn_historical_amount.setText(f"¥{hist_amt} ({hist_cnt}笔)")

        self.edit_title.setText(data.get("title", ""))
        self.edit_budget.setText(str(data.get("budget_amount", "0.00")))
        self.edit_profile.setText(data.get("ai_profile", ""))
        self.edit_wechat_remark.setText(data.get("wechat_remark", ""))

    def _on_save_clicked(self):
        if not self.current_phone:
            return

        update_data = {
            "unit_type": self.combo_unit.currentText(),
            "admin_division": self.combo_division.currentText(),
            "purchase_type": self.combo_purchase_type.currentText(),
            "purchase_months": ", ".join(self.combo_purchase_months.get_checked_items()),
            "contact_date": self.edit_contact_date.date().toString("yyyy-MM-dd"),
            "title": self.edit_title.text().strip(),
            "budget_amount": self.edit_budget.text().strip() or "0",
            "ai_profile": self.edit_profile.toPlainText().strip(),
            "wechat_remark": self.edit_wechat_remark.text().strip(),
        }
        self.save_clicked.emit(self.current_phone, update_data)
