"""
客户全息档案面板：CustomerInfoWidget
对应 UI_implementation.md Phase 3 — 客户信息表单改造
"""
import os
import json
import re

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QFrame,
)
from PySide6.QtCore import Qt, Signal, QDate
from logger_cfg import logger

from qfluentwidgets import (
    SubtitleLabel, LineEdit, TextEdit, ComboBox, EditableComboBox,
    PrimaryPushButton, TransparentPushButton, ZhDatePicker, isDarkTheme, themeColor
)

from ui.widgets.form_controls import NoScrollComboBox, MultiSelectComboBox, ProfileTagMultiSelectComboBox
from ui.widgets.cascader import RegionCascader
from utils import get_resource_path


class CustomerInfoWidget(QWidget):
    """
    客户详情信息面板：视觉风格大一统。
    """
    save_clicked = Signal(int, str, dict)
    history_clicked = Signal(int)  # 传递客户 ID

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 10, 15, 10)
        layout.setSpacing(10)

        # 统一表单样式
        self.form_container = QFrame()
        self.form_container.setObjectName("FormContainer")
        
        form_layout = QFormLayout(self.form_container)
        form_layout.setLabelAlignment(Qt.AlignRight)
        form_layout.setVerticalSpacing(6)
        form_layout.setHorizontalSpacing(15)

        # 1. 核心客观字段（可编辑，保存时同步至 Customer 主表）
        self.edit_name = LineEdit()
        self.edit_name.setPlaceholderText("客户真实姓名")
        self.edit_phone = LineEdit()
        self.edit_phone.setPlaceholderText("联系电话（可留空；勿与他人重复）")

        # 2. 动态选项与组件
        self.combo_unit = EditableComboBox()
        self.combo_unit.setPlaceholderText("请选择或输入所属单位...")
        self.combo_purchase_type = EditableComboBox()
        self.combo_purchase_type.setPlaceholderText("请选择或输入采购模式...")
        self.edit_wechat_remark = LineEdit()
        self.edit_wechat_remark.setPlaceholderText("填入客户的微信备注")

        # 加载本地城市数据
        self._pca_data = {}
        pca_path = get_resource_path("pca.json")
        if os.path.exists(pca_path):
            with open(pca_path, "r", encoding="utf-8") as f:
                self._pca_data = json.load(f)

        self.combo_division = RegionCascader(self._pca_data)
        self.edit_contact_date = ZhDatePicker()
        self.edit_followup_date = ZhDatePicker()
        self.combo_purchase_months = MultiSelectComboBox()

        self.btn_historical_amount = TransparentPushButton("0.00 元")
        self.btn_historical_amount.clicked.connect(
            lambda: self.history_clicked.emit(self.current_customer_id) if self.current_customer_id else None
        )

        # 3. 业务主观字段
        self.edit_title = LineEdit()
        self.edit_title.setPlaceholderText("例如：李局、张总")
        self.edit_budget = LineEdit()
        self.edit_budget.setPlaceholderText("预计单笔采购预算")
        self.edit_profile = TextEdit()
        self.edit_profile.setPlaceholderText("性格、偏好、历史沟通记录...")
        self.edit_profile.setMinimumHeight(100)

        self.combo_profile_tags = ProfileTagMultiSelectComboBox()
        self._profile_tag_catalog: list = []

        form_layout.addRow("真实姓名:", self.edit_name)
        form_layout.addRow("联系电话:", self.edit_phone)
        form_layout.addRow("微信备注:", self.edit_wechat_remark)
        form_layout.addRow("所属单位:", self.combo_unit)
        form_layout.addRow("行政区划:", self.combo_division)
        form_layout.addRow("建联日期:", self.edit_contact_date)
        form_layout.addRow("建议跟进:", self.edit_followup_date)
        form_layout.addRow("采购类型:", self.combo_purchase_type)
        form_layout.addRow("采货月份:", self.combo_purchase_months)
        form_layout.addRow("历史总额:", self.btn_historical_amount)
        form_layout.addRow("当前称呼:", self.edit_title)
        form_layout.addRow("采购预算:", self.edit_budget)
        form_layout.addRow("动态标签:", self.combo_profile_tags)
        form_layout.addRow("私域画像:", self.edit_profile)

        layout.addWidget(self.form_container)

        self.save_btn = PrimaryPushButton("保存全部跟进信息")
        self.save_btn.setFixedHeight(36)
        self.save_btn.clicked.connect(self._on_save_clicked)
        layout.addWidget(self.save_btn)

        layout.addStretch()
        self.current_phone = None
        self.current_customer_id = None
        self._lookup_phone = None

        self._apply_theme_style()

    def set_profile_tag_catalog(self, items: list):
        """登录后拉取的启用标签列表；与客户当前标签合并展示。"""
        self._profile_tag_catalog = list(items or [])

    def _placeholder_theme_removed(self):
        pass  # 旧样式方法已整合至文件底部的 _apply_theme_style

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
        self._lookup_phone = data.get("phone")
        self.edit_name.setText(data.get("customer_name") or "")
        self.edit_phone.setText(data.get("phone") or "")

        # 下拉框赋值优化：支持自定义输入
        unit_type = data.get("unit_type", "") or ""
        # 如果预设中没有该选项，动态添加，确保能显示出来
        if unit_type and self.combo_unit.findText(unit_type) == -1:
            self.combo_unit.addItem(unit_type)
        self.combo_unit.setCurrentText(unit_type)

        self.combo_division.setCurrentText(data.get("admin_division", "") or "")

        purchase_type = data.get("purchase_type", "") or ""
        if purchase_type and self.combo_purchase_type.findText(purchase_type) == -1:
            self.combo_purchase_type.addItem(purchase_type)
        self.combo_purchase_type.setCurrentText(purchase_type)

        # 多选框：根据数据长度自适应
        months_str = data.get("purchase_months", "") or ""
        months_list = [m.strip() for m in re.split(r"[,，、;；]+", months_str) if m.strip()]
        self.combo_purchase_months.set_checked_items(months_list)
        if not months_list:
            self.combo_purchase_months.lineEdit().clear()

        contact_dt = data.get("contact_date")
        if contact_dt:
            try:
                year, month, day = map(int, contact_dt.split("-"))
                self.edit_contact_date.date = QDate(year, month, day)
            except Exception as e:
                logger.warning(f"客户建档日期解析失败: {e} ({contact_dt})")

        followup_dt = data.get("suggested_followup_date")
        if followup_dt:
            try:
                year, month, day = map(int, followup_dt.split("-"))
                self.edit_followup_date.date = QDate(year, month, day)
            except Exception as e:
                logger.warning(f"建议跟进日期解析失败: {e} ({followup_dt})")

        hist_amt = data.get("historical_amount", 0.0)
        hist_cnt = data.get("historical_order_count", 0)
        self.btn_historical_amount.setText(f"¥{hist_amt} ({hist_cnt}笔)")

        self.edit_title.setText(data.get("title", ""))
        self.edit_budget.setText(str(data.get("budget_amount", "0.00")))
        self.edit_profile.setText(data.get("ai_profile", ""))
        self.edit_wechat_remark.setText(data.get("wechat_remark", ""))
        self._apply_profile_tags_to_combo(data.get("profile_tags") or [])

    def _apply_profile_tags_to_combo(self, current_tags: list):
        by_id: dict = {}
        for t in self._profile_tag_catalog:
            tid = t.get("id")
            if tid is not None:
                by_id[int(tid)] = t
        for t in current_tags:
            tid = t.get("id")
            if tid is None:
                continue
            tid = int(tid)
            if tid not in by_id:
                by_id[tid] = t
        merged = sorted(by_id.values(), key=lambda x: int(x.get("id", 0)))
        self.combo_profile_tags.set_tag_items(merged)
        sel = [int(t["id"]) for t in current_tags if t.get("id") is not None]
        self.combo_profile_tags.set_checked_tag_ids(sel)
        self._apply_theme_style()

    def _on_save_clicked(self):
        if not self.current_customer_id:
            return

        update_data = {
            "customer_name": self.edit_name.text().strip(),
            "phone": self.edit_phone.text().strip() or None,
            "unit_type": self.combo_unit.currentText(),
            "admin_division": self.combo_division.currentText(),
            "purchase_type": self.combo_purchase_type.currentText(),
            "purchase_months": ", ".join(self.combo_purchase_months.get_checked_items()),
            "contact_date": self.edit_contact_date.date.toString("yyyy-MM-dd") if self.edit_contact_date.date.isValid() else "",
            "suggested_followup_date": self.edit_followup_date.date.toString("yyyy-MM-dd") if self.edit_followup_date.date.isValid() else "",
            "title": self.edit_title.text().strip(),
            "budget_amount": self.edit_budget.text().strip() or "0",
            "ai_profile": self.edit_profile.toPlainText().strip(),
            "wechat_remark": self.edit_wechat_remark.text().strip(),
            "profile_tag_ids": self.combo_profile_tags.get_checked_tag_ids(),
        }
        lookup = self._lookup_phone if self._lookup_phone else ""
        self.save_clicked.emit(self.current_customer_id, lookup, update_data)

    def _apply_theme_style(self):
        """同步抗屉内表单组件与标签的样式"""
        is_dark = isDarkTheme()
        bg = "#272727" if is_dark else "#ffffff"
        border = "#404040" if is_dark else "#e0e0e0"
        lbl_color = "#aaaaaa" if is_dark else "#555555"
        primary_color = themeColor().name()
        # 1. 刷新容器背景
        self.form_container.setStyleSheet(f"QFrame#FormContainer {{ background-color: {bg}; border: none; }}")
        
        # 2. 遗历容器下所有 QLabel，将它们都当做表单标签刷新
        from PySide6.QtWidgets import QLabel as _QLabel, QFormLayout as _QFormLayout
        form_layout = self.form_container.layout()
        if isinstance(form_layout, _QFormLayout):
            for row in range(form_layout.rowCount()):
                lbl_item = form_layout.itemAt(row, _QFormLayout.LabelRole)
                if lbl_item and lbl_item.widget() and isinstance(lbl_item.widget(), _QLabel):
                    lbl_item.widget().setStyleSheet(f"color: {lbl_color}; font-size: 12px;")

        if hasattr(self.combo_profile_tags, "_apply_theme_style"):
            self.combo_profile_tags._apply_theme_style()

        # 3. RegionCascader 样式：模拟 Fluent 输入框风格
        btn_bg = "#373737" if is_dark else "#f9f9f9"
        btn_text = "#dddddd" if is_dark else "#333333"
        self.combo_division.setStyleSheet(
            f"PushButton {{ background-color: {btn_bg}; color: {btn_text}; "
            f"border: 1px solid {border}; border-radius: 5px; "
            f"text-align: left; padding: 0 8px; min-height: 30px; }}"
            f"PushButton:hover {{ border: 1px solid {primary_color}; background-color: {btn_bg}; }}"
            f"PushButton:pressed {{ border: 1px solid {primary_color}; }}"
        )
        
        # 4. MultiSelectComboBox 强制背景同步
        if hasattr(self.combo_purchase_months, "_apply_theme_style"):
            self.combo_purchase_months._apply_theme_style()
        else:
            self.combo_purchase_months.setStyleSheet(
                f"background-color: {bg}; border: 1px solid {border}; border-radius: 5px;"
            )
