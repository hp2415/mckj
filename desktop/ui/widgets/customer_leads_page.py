import sys
import asyncio
from PySide6.QtCore import Qt, Signal, QSize, QDateTime, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidgetItem,
    QDialog, QFrame, QScrollArea, QSizePolicy
)
from qfluentwidgets import (
    SegmentedWidget, ListWidget, SearchLineEdit,
    PushButton, PrimaryPushButton, TransparentPushButton,
    StrongBodyLabel, BodyLabel, CaptionLabel, SwitchButton,
    isDarkTheme, InfoBar, InfoBarPosition, TextEdit,
    ComboBox, LineEdit
)
from ui.widgets.form_controls import MultiSelectComboBox, CalendarDateTimePicker, parse_followup_datetime
from utils import mask_phone

# Mock Initial Data
MOCK_CLAIMED_LEADS = [
    {
        "id": 1,
        "unit_name": "平罗县灵沙中心学校",
        "customer_name": "未知",
        "phone": "13766666182",
        "region": "宁夏回族自治区 / 石嘴山市 / 平罗县",
        "last_call_time": "-",
        "tags": "待设置",
        "color": "灰色",
        "purchase_month": "待设置",
        "followup_time": "待设置",
        "wechat_id": "待设置",
        "budget": "待设置",
        "is_favorite": False,
        "purchase_type": "待设置",
        "recycle_days": "390天",
        "allocation_time": "2026-06-05 10:30:12",
        "followup_records": []
    },
    {
        "id": 2,
        "unit_name": "岳阳县扶贫开发办公室",
        "customer_name": "李主任",
        "phone": "13955555420",
        "region": "湖南省 / 岳阳市 / 岳阳县",
        "last_call_time": "2026-06-04 15:30:00",
        "tags": "意向客户",
        "color": "蓝色",
        "purchase_month": "6月",
        "followup_time": "2026-06-10",
        "wechat_id": "wx_lee420",
        "budget": "50,000 元",
        "is_favorite": True,
        "purchase_type": "政府集采",
        "recycle_days": "300天",
        "allocation_time": "2026-06-05 09:15:00",
        "followup_records": [
            {"time": "2026-06-04 15:32:00", "content": "电话联系李主任，对方表达了对本期采购意愿，约下周具体面谈。"}
        ]
    },
    {
        "id": 3,
        "unit_name": "石嘴山市农村经济发展局",
        "customer_name": "未知",
        "phone": "13788888203",
        "region": "宁夏回族自治区 / 石嘴山市 / 大武口区",
        "last_call_time": "-",
        "tags": "待设置",
        "color": "灰色",
        "purchase_month": "待设置",
        "followup_time": "待设置",
        "wechat_id": "待设置",
        "budget": "待设置",
        "is_favorite": False,
        "purchase_type": "待设置",
        "recycle_days": "390天",
        "allocation_time": "2026-06-05 10:30:12",
        "followup_records": []
    },
    {
        "id": 4,
        "unit_name": "石嘴山市人民防空办公室",
        "customer_name": "未知",
        "phone": "13566666198",
        "region": "宁夏回族自治区 / 石嘴山市 / 大武口区",
        "last_call_time": "-",
        "tags": "待设置",
        "color": "灰色",
        "purchase_month": "待设置",
        "followup_time": "待设置",
        "wechat_id": "待设置",
        "budget": "待设置",
        "is_favorite": False,
        "purchase_type": "待设置",
        "recycle_days": "390天",
        "allocation_time": "2026-06-05 10:30:12",
        "followup_records": []
    },
    {
        "id": 5,
        "unit_name": "同心县丁塘镇中心学校",
        "customer_name": "未知",
        "phone": "13588888222",
        "region": "宁夏回族自治区 / 吴忠市 / 同心县",
        "last_call_time": "-",
        "tags": "待设置",
        "color": "灰色",
        "purchase_month": "待设置",
        "followup_time": "待设置",
        "wechat_id": "待设置",
        "budget": "待设置",
        "is_favorite": False,
        "purchase_type": "待设置",
        "recycle_days": "390天",
        "allocation_time": "2026-06-05 10:30:12",
        "followup_records": []
    }
]

MOCK_FAVORITE_LEADS = [
    {
        "id": 101,
        "unit_name": "新疆维吾尔自治区拜城县气象局",
        "customer_name": "未知",
        "phone": "18099999172",
        "region": "新疆维吾尔自治区 / 阿克苏地区 / 拜城县",
        "last_call_time": "2026-04-08 10:02:11",
        "tags": "未设置",
        "color": "灰色",
        "purchase_month": "2月",
        "followup_time": "2026-08-01",
        "wechat_id": "待设置",
        "budget": "待设置",
        "is_favorite": True,
        "purchase_type": "未设置",
        "favorite_time": "2024-07-30 11:22:33",
        "followup_records": [
            {"time": "2026-04-08 10:02:11", "content": "这是最新的备注信息，客户说后续采购在明年初。"}
        ]
    },
    {
        "id": 102,
        "unit_name": "巴音郭楞蒙古自治州中心卫生院",
        "customer_name": "未知",
        "phone": "18088888420",
        "region": "新疆维吾尔自治区 / 巴音郭楞蒙古自治州",
        "last_call_time": "-",
        "tags": "60天后两品符下单",
        "color": "橙色",
        "purchase_month": "待设置",
        "followup_time": "2026-08-01",
        "wechat_id": "待设置",
        "budget": "待设置",
        "is_favorite": True,
        "purchase_type": "未设置",
        "favorite_time": "2025-08-01 09:15:00",
        "followup_records": []
    },
    {
        "id": 103,
        "unit_name": "中国人民银行沂水县支行",
        "customer_name": "黄湾",
        "phone": "13477777600",
        "region": "山东省 / 临沂市 / 沂水县",
        "last_call_time": "2026-05-12 14:00:00",
        "tags": "未设置",
        "color": "灰色",
        "purchase_month": "待设置",
        "followup_time": "设置",
        "wechat_id": "待设置",
        "budget": "待设置",
        "is_favorite": True,
        "purchase_type": "未设置",
        "favorite_time": "2024-08-12 14:20:00",
        "followup_records": []
    },
    {
        "id": 104,
        "unit_name": "中共喀什市委组织部",
        "customer_name": "未知",
        "phone": "18066666511",
        "region": "新疆维吾尔自治区 / 喀什地区 / 喀什市",
        "last_call_time": "-",
        "tags": "40本周内采购",
        "color": "蓝色",
        "purchase_month": "待设置",
        "followup_time": "2026-07-10",
        "wechat_id": "待设置",
        "budget": "待设置",
        "is_favorite": True,
        "purchase_type": "未设置",
        "favorite_time": "2024-07-08 17:35:00",
        "followup_records": []
    }
]


class LeadDetailDialog(QDialog):
    """
    客资详细资料与跟进记录对话框 (详情页弹窗)
    高度还原 screenshot 1 风格
    """
    def __init__(self, lead_data: dict, parent=None):
        super().__init__(parent)
        self.lead_data = lead_data
        self.full_phone_shown = False
        unit_name = lead_data.get('unit_name') or '未知单位'
        self.setWindowTitle(f"【{unit_name}】客资详情")
        self.resize(480, 680)
        self.setMinimumSize(400, 600)
        
        # UI Layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # 1. 客资详情区域（使用系统原生标题栏与关闭按钮）
        self.details_container = QFrame()
        self.details_container.setObjectName("DetailsContainer")
        details_layout = QVBoxLayout(self.details_container)
        details_layout.setContentsMargins(15, 10, 15, 10)
        details_layout.setSpacing(8)
        
        # Basic labels
        unit_lbl = QLabel(f"单位名称: {lead_data.get('unit_name')}")
        
        # Contact line
        contact_layout = QHBoxLayout()
        contact_layout.setContentsMargins(0, 0, 0, 0)
        contact_layout.setSpacing(5)
        self.phone_mask = mask_phone(lead_data.get('phone', ''))
        self.contact_lbl = QLabel(f"联系人: {lead_data.get('customer_name')} {self.phone_mask}")
        self.view_full_btn = TransparentPushButton("查看完整号码")
        self.view_full_btn.setStyleSheet("font-size: 11px; color: #07c160;")
        self.view_full_btn.clicked.connect(self._toggle_phone_display)
        contact_layout.addWidget(self.contact_lbl)
        contact_layout.addWidget(self.view_full_btn)
        contact_layout.addStretch()
        
        region_lbl = QLabel(f"地区: {lead_data.get('region')}")
        
        remarks_title = StrongBodyLabel("备注")
        
        self.tag_combo = ComboBox()
        self.tag_combo.addItems(["待设置", "20不反感可跟进", "30本月内采购", "40本周内采购", "60选定商品待下单", "80已下单待发货", "停机", "暂停服务", "负责人更换", "拒绝", "未接通"])
        self.tag_combo.setCurrentText(lead_data.get('tags', '待设置'))
        
        self.color_combo = ComboBox()
        self.color_combo.addItems(["灰色", "红色", "蓝色", "橙色", "黄色", "绿色"])
        self.color_combo.setCurrentText(lead_data.get('color', '灰色'))
        
        self.month_combo = MultiSelectComboBox()
        self.month_combo.addItemsChecked([f"{i}月" for i in range(1, 13)])
        month_str = lead_data.get('purchase_month', '')
        if month_str and month_str != "待设置":
            months_list = [m.strip() for m in month_str.split(",") if m.strip()]
            self.month_combo.set_checked_items(months_list)
        
        self.followup_picker = CalendarDateTimePicker()
        follow_dt = parse_followup_datetime(lead_data.get('followup_time', ''))
        if follow_dt:
            self.followup_picker.datetime = follow_dt
        else:
            self.followup_picker.clear()
        
        self.wechat_edit = LineEdit()
        self.wechat_edit.setPlaceholderText("请输入微信账号...")
        wechat_val = lead_data.get('wechat_id', '待设置')
        self.wechat_edit.setText("" if wechat_val == "待设置" else wechat_val)
        
        self.budget_edit = LineEdit()
        self.budget_edit.setPlaceholderText("请输入预算金额...")
        budget_val = lead_data.get('budget', '待设置')
        self.budget_edit.setText("" if budget_val == "待设置" else budget_val)
        
        self.fav_switch = SwitchButton()
        self.fav_switch.setChecked(lead_data.get('is_favorite', False))
        self.fav_switch.checkedChanged.connect(self._toggle_favorite)
        
        self.type_combo = ComboBox()
        self.type_combo.addItems(["工会", "食堂", "工会+食堂", "其他", "待设置"])
        type_val = lead_data.get('purchase_type', '待设置')
        self.type_combo.setCurrentText(type_val)

        _ctrl_h = 32
        for ctrl in (
            self.tag_combo, self.color_combo, self.month_combo,
            self.wechat_edit, self.budget_edit, self.type_combo,
        ):
            ctrl.setFixedHeight(_ctrl_h)
        self.followup_picker.setFixedHeight(_ctrl_h)
        self.followup_picker.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.followup_picker.custom_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        
        remarks_card = QFrame()
        remarks_card.setObjectName("RemarksCard")
        remarks_layout = QVBoxLayout(remarks_card)
        remarks_layout.setContentsMargins(12, 10, 12, 10)
        remarks_layout.setSpacing(10)
        
        call_time_lbl = CaptionLabel(f"最近呼叫: {lead_data.get('last_call_time') or '-'}")
        remarks_layout.addWidget(call_time_lbl)
        
        tag_color_row = QHBoxLayout()
        tag_color_row.setSpacing(12)
        tag_block = QVBoxLayout()
        tag_block.setSpacing(4)
        tag_block.addWidget(CaptionLabel("标签"))
        tag_block.addWidget(self.tag_combo)
        color_block = QVBoxLayout()
        color_block.setSpacing(4)
        color_block.addWidget(CaptionLabel("颜色"))
        color_block.addWidget(self.color_combo)
        tag_color_row.addLayout(tag_block, 1)
        tag_color_row.addLayout(color_block, 1)
        remarks_layout.addLayout(tag_color_row)
        
        month_followup_row = QHBoxLayout()
        month_followup_row.setSpacing(12)
        month_block = QVBoxLayout()
        month_block.setSpacing(4)
        month_block.addWidget(CaptionLabel("采购月份"))
        month_block.addWidget(self.month_combo)
        followup_block = QVBoxLayout()
        followup_block.setSpacing(4)
        followup_block.addWidget(CaptionLabel("回访时间"))
        followup_block.addWidget(self.followup_picker)
        month_followup_row.addLayout(month_block, 2)
        month_followup_row.addLayout(followup_block, 3)
        remarks_layout.addLayout(month_followup_row)
        
        wechat_budget_row = QHBoxLayout()
        wechat_budget_row.setSpacing(12)
        wechat_block = QVBoxLayout()
        wechat_block.setSpacing(4)
        wechat_block.addWidget(CaptionLabel("微信账号"))
        wechat_block.addWidget(self.wechat_edit)
        budget_block = QVBoxLayout()
        budget_block.setSpacing(4)
        budget_block.addWidget(CaptionLabel("预算金额"))
        budget_block.addWidget(self.budget_edit)
        wechat_budget_row.addLayout(wechat_block, 1)
        wechat_budget_row.addLayout(budget_block, 1)
        remarks_layout.addLayout(wechat_budget_row)
        
        type_fav_row = QHBoxLayout()
        type_fav_row.setSpacing(12)
        type_block = QVBoxLayout()
        type_block.setSpacing(4)
        type_block.addWidget(CaptionLabel("采购类型"))
        type_block.addWidget(self.type_combo)
        fav_block = QVBoxLayout()
        fav_block.setSpacing(4)
        fav_block.addWidget(CaptionLabel("收藏"))
        fav_switch_row = QHBoxLayout()
        fav_switch_row.setContentsMargins(0, 4, 0, 0)
        fav_switch_row.addWidget(self.fav_switch)
        fav_switch_row.addStretch()
        fav_block.addLayout(fav_switch_row)
        type_fav_row.addLayout(type_block, 1)
        type_fav_row.addLayout(fav_block, 1)
        remarks_layout.addLayout(type_fav_row)
        
        self.confirm_btn = PrimaryPushButton("确认")
        self.confirm_btn.setFixedHeight(36)
        self.confirm_btn.clicked.connect(self._on_confirm_clicked)
        confirm_row = QHBoxLayout()
        confirm_row.setContentsMargins(0, 4, 0, 0)
        confirm_row.addStretch()
        confirm_row.addWidget(self.confirm_btn)
        confirm_row.addStretch()
        
        details_layout.addWidget(unit_lbl)
        details_layout.addLayout(contact_layout)
        details_layout.addWidget(region_lbl)
        details_layout.addWidget(remarks_title)
        details_layout.addWidget(remarks_card)
        details_layout.addLayout(confirm_row)
        
        main_layout.addWidget(self.details_container)
        
        # Divider Line
        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setFrameShadow(QFrame.Sunken)
        divider.setStyleSheet("background-color: rgba(0, 0, 0, 0.08); max-height: 1px; border: none;")
        main_layout.addWidget(divider)
        
        # 3. 跟进记录展示区
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("border: none; background: transparent;")
        
        self.records_content = QWidget()
        self.records_content.setObjectName("TimelineContent")
        self.timeline_layout = QVBoxLayout(self.records_content)
        self.timeline_layout.setContentsMargins(15, 15, 15, 15)
        self.timeline_layout.setSpacing(10)
        self.timeline_layout.addStretch(1)
        
        self.scroll_area.setWidget(self.records_content)
        main_layout.addWidget(self.scroll_area, 1)
        
        # Placeholder for empty records
        self.empty_lbl = QLabel()
        self.empty_lbl.setAlignment(Qt.AlignCenter)
        self.empty_lbl.setWordWrap(True)
        self.empty_lbl.setText("还没有任何跟进记录")
        self.empty_lbl.setStyleSheet("color: #8c8c8c; font-size: 13px; margin: 30px;")
        
        # Add timeline elements
        self._refresh_timeline()
        
        # Divider Line
        divider2 = QFrame()
        divider2.setFrameShape(QFrame.HLine)
        divider2.setFrameShadow(QFrame.Sunken)
        divider2.setStyleSheet("background-color: rgba(0, 0, 0, 0.08); max-height: 1px; border: none;")
        main_layout.addWidget(divider2)
        
        # 4. 底部触发条（输入面板以浮层覆盖在上方，不参与布局挤压）
        self.input_frame = QFrame()
        self.input_frame.setObjectName("InputFrame")
        input_layout = QVBoxLayout(self.input_frame)
        input_layout.setContentsMargins(15, 8, 15, 10)
        input_layout.setSpacing(0)

        self.toggle_followup_btn = TransparentPushButton("＋ 添加跟进内容")
        self.toggle_followup_btn.setFixedHeight(32)
        self.toggle_followup_btn.clicked.connect(self._toggle_followup_input)
        input_layout.addWidget(self.toggle_followup_btn)
        main_layout.addWidget(self.input_frame)

        self._followup_overlay_height = 156
        self.followup_overlay = QFrame(self)
        self.followup_overlay.setObjectName("FollowupOverlay")
        self.followup_overlay.setVisible(False)
        overlay_layout = QVBoxLayout(self.followup_overlay)
        overlay_layout.setContentsMargins(15, 10, 15, 10)
        overlay_layout.setSpacing(8)

        self.note_edit = TextEdit()
        self.note_edit.setPlaceholderText("请输入跟进内容...")
        self.note_edit.setFixedHeight(88)
        self.note_edit.textChanged.connect(self._update_word_count)

        self.count_lbl = CaptionLabel("0/500")
        self.count_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(8)

        self.add_note_btn = PrimaryPushButton("立即添加")
        self.add_note_btn.setFixedHeight(32)
        self.add_note_btn.clicked.connect(self._add_followup_note)
        self.clear_btn = PushButton("清空")
        self.clear_btn.setFixedHeight(32)
        self.clear_btn.clicked.connect(self._clear_input)

        btn_layout.addWidget(self.add_note_btn)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.count_lbl)

        overlay_layout.addWidget(self.note_edit)
        overlay_layout.addLayout(btn_layout)
        
        self._apply_theme_style()

    def _toggle_phone_display(self):
        self.full_phone_shown = not self.full_phone_shown
        if self.full_phone_shown:
            self.contact_lbl.setText(f"联系人: {self.lead_data.get('customer_name')} {self.lead_data.get('phone')}")
            self.view_full_btn.setText("隐藏完整号码")
        else:
            self.contact_lbl.setText(f"联系人: {self.lead_data.get('customer_name')} {self.phone_mask}")
            self.view_full_btn.setText("查看完整号码")

    def _toggle_favorite(self, checked):
        self.lead_data['is_favorite'] = checked

    def _on_confirm_clicked(self):
        self._save_attributes()
        InfoBar.success(
            title="保存成功",
            content="备注信息已更新",
            duration=2000,
            position=InfoBarPosition.TOP,
            parent=self.parentWidget() or self,
        )
        QTimer.singleShot(200, self.close)

    def _save_attributes(self):
        self.lead_data['tags'] = self.tag_combo.currentText()
        self.lead_data['color'] = self.color_combo.currentText()
        self.lead_data['purchase_month'] = ", ".join(self.month_combo.get_checked_items()) or "待设置"
        follow_dt = self.followup_picker.datetime
        self.lead_data['followup_time'] = (
            follow_dt.toString("yyyy-MM-dd HH:mm:ss") if follow_dt.isValid() else "待设置"
        )
        self.lead_data['wechat_id'] = self.wechat_edit.text().strip() or "待设置"
        self.lead_data['budget'] = self.budget_edit.text().strip() or "待设置"
        self.lead_data['purchase_type'] = self.type_combo.currentText()
        
        lead_id = self.lead_data['id']
        for lead in MOCK_CLAIMED_LEADS + MOCK_FAVORITE_LEADS:
            if lead['id'] == lead_id:
                lead.update(self.lead_data)
                break

    def closeEvent(self, event):
        self._save_attributes()
        super().closeEvent(event)

    def close(self):
        self._save_attributes()
        super().close()

    def _update_word_count(self):
        text_len = len(self.note_edit.toPlainText())
        self.count_lbl.setText(f"{text_len}/500")
        if text_len > 500:
            self.count_lbl.setStyleSheet("color: red;")
            self.add_note_btn.setEnabled(False)
        else:
            self.count_lbl.setStyleSheet("")
            self.add_note_btn.setEnabled(True)

    def _clear_input(self):
        self.note_edit.clear()

    def _position_followup_overlay(self):
        bottom_h = self.input_frame.height()
        overlay_h = self._followup_overlay_height
        self.followup_overlay.setGeometry(
            0,
            max(0, self.height() - bottom_h - overlay_h),
            self.width(),
            overlay_h,
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "followup_overlay") and self.followup_overlay.isVisible():
            self._position_followup_overlay()

    def _toggle_followup_input(self):
        if self.followup_overlay.isVisible():
            self._collapse_followup_input()
        else:
            self._position_followup_overlay()
            self.followup_overlay.setVisible(True)
            self.followup_overlay.raise_()
            self.toggle_followup_btn.setText("收起跟进输入")
            self.note_edit.setFocus()

    def _collapse_followup_input(self):
        self.followup_overlay.setVisible(False)
        self.toggle_followup_btn.setText("＋ 添加跟进内容")

    def _add_followup_note(self):
        text = self.note_edit.toPlainText().strip()
        if not text:
            return
        
        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_record = {"time": now_str, "content": text}
        self.lead_data['followup_records'].insert(0, new_record)
        
        # Sync back to mock list
        for lead in MOCK_CLAIMED_LEADS + MOCK_FAVORITE_LEADS:
            if lead['id'] == self.lead_data['id']:
                lead['followup_records'] = self.lead_data['followup_records']
                break
                
        self.note_edit.clear()
        self._refresh_timeline()
        self._collapse_followup_input()

        InfoBar.success(
            title="添加成功",
            content="跟进记录已成功添加",
            duration=2000,
            position=InfoBarPosition.TOP,
            parent=self.parentWidget() or self,
        )

    def _refresh_timeline(self):
        # Clear existing timeline items except the stretch
        while self.timeline_layout.count() > 1:
            item = self.timeline_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
                
        records = self.lead_data.get('followup_records', [])
        if not records:
            self.timeline_layout.insertWidget(0, self.empty_lbl)
            self.empty_lbl.show()
        else:
            self.empty_lbl.hide()
            for record in reversed(records): # Display bottom-to-top chronological or reverse
                card = QFrame()
                card.setObjectName("TimelineCard")
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(10, 8, 10, 8)
                card_layout.setSpacing(4)
                
                time_lbl = CaptionLabel(record['time'])
                time_lbl.setStyleSheet("color: #07c160; font-weight: bold;")
                content_lbl = BodyLabel(record['content'])
                content_lbl.setWordWrap(True)
                
                card_layout.addWidget(time_lbl)
                card_layout.addWidget(content_lbl)
                self.timeline_layout.insertWidget(0, card)
                
    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        bg_color = "#272727" if is_dark else "#fdfdfd"
        card_bg = "#303030" if is_dark else "#f5f5f5"
        text_color = "#eeeeee" if is_dark else "#333333"
        border_color = "rgba(255,255,255,0.08)" if is_dark else "rgba(0,0,0,0.08)"
        
        if hasattr(self, 'month_combo') and hasattr(self.month_combo, '_apply_theme_style'):
            self.month_combo._apply_theme_style()
            
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {bg_color};
            }}
            QFrame#DetailsContainer {{
                background-color: {bg_color};
            }}
            QFrame#RemarksCard {{
                background-color: {card_bg};
                border: 1px solid {border_color};
                border-radius: 8px;
            }}
            QLabel {{
                color: {text_color};
                font-size: 12px;
            }}
            QFrame#TimelineCard {{
                background-color: {card_bg};
                border: 1px solid {border_color};
                border-radius: 6px;
            }}
            QFrame#InputFrame {{
                background-color: {card_bg};
                border-top: 1px solid {border_color};
            }}
            QFrame#FollowupOverlay {{
                background-color: {card_bg};
                border-top: 1px solid {border_color};
            }}
        """)


class LeadCardWidget(QFrame):
    """
    客资卡片：以卡片形式展示客资基础信息，点击可进入详情
    """
    detail_requested = Signal(dict)
    remove_requested = Signal(dict)

    def __init__(self, lead_data: dict, is_claimed: bool, parent=None):
        super().__init__(parent)
        self.lead_data = lead_data
        self.is_claimed = is_claimed
        self.setObjectName("LeadCard")
        self.setFrameShape(QFrame.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        
        # Layout
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)
        
        # 1. Title Row: Unit Name
        self.unit_lbl = StrongBodyLabel(lead_data.get('unit_name', '未知单位'))
        self.unit_lbl.setWordWrap(True)
        root.addWidget(self.unit_lbl)
        
        # 2. Details Row: Contact & Masked Phone & Region
        masked_phone = mask_phone(lead_data.get('phone', ''))
        info_str = f"联系人: {lead_data.get('customer_name')} | {masked_phone}   地区: {lead_data.get('region')}"
        self.info_lbl = CaptionLabel(info_str)
        self.info_lbl.setWordWrap(True)
        root.addWidget(self.info_lbl)
        
        # 3. Extra Attributes: Tags, Budget
        extra_parts = []
        tags = lead_data.get('tags')
        if tags and tags != "待设置":
            extra_parts.append(f"标签: {tags}")
        budget = lead_data.get('budget')
        if budget and budget != "待设置":
            extra_parts.append(f"预算: {budget}")
        last_call = lead_data.get('last_call_time')
        if last_call and last_call != "-":
            extra_parts.append(f"最近呼叫: {last_call}")
            
        if extra_parts:
            self.extra_lbl = CaptionLabel("  ·  ".join(extra_parts))
            root.addWidget(self.extra_lbl)
            
        # 4. Time Information Row
        self.time_lbl = CaptionLabel()
        if self.is_claimed:
            self.time_lbl.setText(f"分配时间: {lead_data.get('allocation_time', '-')}    回收倒计时: {lead_data.get('recycle_days', '390天')}")
            self.time_lbl.setStyleSheet("color: #ff4d4f; font-weight: bold;")
        else:
            self.time_lbl.setText(f"收藏时间: {lead_data.get('favorite_time', '-')}")
            self.time_lbl.setStyleSheet("color: #722ed1; font-weight: bold;")
        root.addWidget(self.time_lbl)
            
        # 5. Footer Buttons Row
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 2, 0, 0)
        footer_layout.setSpacing(8)
        footer_layout.addStretch(1)
        
        # Dial Buttons
        self.call1_btn = PushButton("畅呼外呼")
        self.call1_btn.setFixedHeight(24)
        self.call1_btn.setToolTip("通过畅呼系统拨打外呼电话")
        self.call1_btn.clicked.connect(self._on_call1_clicked)
        
        self.call2_btn = PushButton("云客外呼")
        self.call2_btn.setFixedHeight(24)
        self.call2_btn.setToolTip("通过云客系统拨打外呼电话")
        self.call2_btn.clicked.connect(self._on_call2_clicked)
        
        # Detail Button
        self.detail_btn = PushButton("详情")
        self.detail_btn.setFixedHeight(24)
        self.detail_btn.clicked.connect(self._on_detail_clicked)
        
        footer_layout.addWidget(self.call1_btn)
        footer_layout.addWidget(self.call2_btn)
        footer_layout.addWidget(self.detail_btn)
        
        if self.is_claimed:
            # Remove Button
            self.remove_btn = PushButton("移除")
            self.remove_btn.setFixedHeight(24)
            self.remove_btn.setStyleSheet("QPushButton { color: #ff4d4f; } QPushButton:hover { border-color: #ff4d4f; }")
            self.remove_btn.clicked.connect(self._on_remove_clicked)
            footer_layout.addWidget(self.remove_btn)
            
        root.addLayout(footer_layout)
        self._apply_theme_style()

    def _on_call1_clicked(self):
        from datetime import datetime
        self.lead_data['last_call_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        InfoBar.success(
            title="畅呼拨号",
            content=f"已成功唤起畅呼，拨打 {mask_phone(self.lead_data.get('phone', ''))}",
            duration=2000,
            position=InfoBarPosition.TOP,
            parent=self.window()
        )
        self.detail_requested.emit(self.lead_data)

    def _on_call2_clicked(self):
        from datetime import datetime
        self.lead_data['last_call_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        InfoBar.success(
            title="云客拨号",
            content=f"已成功唤起云客，拨打 {mask_phone(self.lead_data.get('phone', ''))}",
            duration=2000,
            position=InfoBarPosition.TOP,
            parent=self.window()
        )
        self.detail_requested.emit(self.lead_data)

    def _on_detail_clicked(self):
        self.detail_requested.emit(self.lead_data)

    def _on_remove_clicked(self):
        self.remove_requested.emit(self.lead_data)
        
    def mousePressEvent(self, event):
        # Click on card body (not child buttons) also opens detail
        if event.button() == Qt.LeftButton:
            child = self.childAt(event.pos())
            if not isinstance(child, PushButton):
                self.detail_requested.emit(self.lead_data)
                event.accept()
                return
        super().mousePressEvent(event)

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        card_bg = "#2e2e2e" if is_dark else "#ffffff"
        card_border = "rgba(255,255,255,0.12)" if is_dark else "rgba(0,0,0,0.09)"
        text_main = "#e8e8e8" if is_dark else "#1a1a1a"
        text_sub = "#999999" if is_dark else "#666666"
        
        # Map color names to actual hex codes
        COLOR_MAP = {
            "灰色": "#8c8c8c",
            "红色": "#ff4d4f",
            "蓝色": "#1890ff",
            "橙色": "#fa8c16",
            "黄色": "#fadb14",
            "绿色": "#52c41a",
        }
        color_name = self.lead_data.get('color', '灰色')
        side_color = COLOR_MAP.get(color_name, "#8c8c8c")
        
        self.setStyleSheet(f"""
            QFrame#LeadCard {{
                background-color: {card_bg};
                border: 1px solid {card_border};
                border-left: 4px solid {side_color};
                border-radius: 8px;
            }}
        """)
        self.unit_lbl.setStyleSheet(f"color: {text_main}; font-size: 13px; font-weight: bold;")
        self.info_lbl.setStyleSheet(f"color: {text_sub}; font-size: 11px;")
        if hasattr(self, 'extra_lbl'):
            self.extra_lbl.setStyleSheet(f"color: {text_sub}; font-size: 11px;")
            
        if is_dark:
            btn_fg = "#cccccc"
            btn_bg = "rgba(255,255,255,0.07)"
            btn_border = "rgba(255,255,255,0.15)"
            btn_hover = "rgba(255,255,255,0.14)"
        else:
            btn_fg = "#444444"
            btn_bg = "rgba(0,0,0,0.04)"
            btn_border = "rgba(0,0,0,0.12)"
            btn_hover = "rgba(0,0,0,0.09)"
        btn_style = (
            f"QPushButton {{ color: {btn_fg}; background-color: {btn_bg};"
            f" border: 1px solid {btn_border}; border-radius: 4px;"
            f" padding: 1px 10px; font-size: 11px; }}"
            f"QPushButton:hover {{ background-color: {btn_hover}; border-color: #07c160; }}"
            f"QPushButton:pressed {{ background-color: rgba(7,193,96,0.18);"
            f" border-color: #07c160; color: #07c160; }}"
        )
        self.call1_btn.setStyleSheet(btn_style)
        self.call2_btn.setStyleSheet(btn_style)
        self.detail_btn.setStyleSheet(btn_style)
        if self.is_claimed:
            remove_style = (
                f"QPushButton {{ color: #ff7875; background-color: {btn_bg};"
                f" border: 1px solid {btn_border}; border-radius: 4px;"
                f" padding: 1px 10px; font-size: 11px; }}"
                f"QPushButton:hover {{ background-color: rgba(255,77,79,0.15);"
                f" border-color: #ff4d4f; color: #ff4d4f; }}"
            )
            self.remove_btn.setStyleSheet(remove_style)


class CustomerLeadsWidget(QFrame):
    """
    客资列表页主 Widget (整合认领客资、收藏客资两部分)
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CustomerLeadsPage")
        self.setFrameShape(QFrame.NoFrame)
        
        # Copy mock lists to allow runtime operations
        self.claimed_leads = list(MOCK_CLAIMED_LEADS)
        self.favorite_leads = list(MOCK_FAVORITE_LEADS)
        
        self.current_tab = "claimed" # "claimed" or "favorite"
        
        # Root Layout
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(15, 12, 15, 12)
        root_layout.setSpacing(10)

        # 1. 头部导航（固定顶部，不随空列表下沉）
        self.header_area = QWidget()
        self.header_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        header_layout = QVBoxLayout(self.header_area)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(10)

        title_layout = QHBoxLayout()
        self.title_lbl = StrongBodyLabel("客资列表")
        self.title_lbl.setStyleSheet("font-size: 18px;")
        title_layout.addWidget(self.title_lbl)
        title_layout.addStretch()
        header_layout.addLayout(title_layout)

        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(15)

        self.segmented_tab = SegmentedWidget()
        self.segmented_tab.setMinimumWidth(200)
        self.segmented_tab.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.segmented_tab.addItem("claimed", "认领客资", self._switch_to_claimed)
        self.segmented_tab.addItem("favorite", "收藏客资", self._switch_to_favorite)

        self.search_box = SearchLineEdit()
        self.search_box.setPlaceholderText("搜索单位、姓名或电话...")
        self.search_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.search_box.textChanged.connect(self._filter_list)

        controls_layout.addWidget(self.segmented_tab)
        controls_layout.addStretch(1)
        controls_layout.addWidget(self.search_box)
        header_layout.addLayout(controls_layout)

        root_layout.addWidget(self.header_area, 0)

        # 2. 列表区域（空状态提示固定在内容区顶部）
        self.list_area = QWidget()
        self.list_area.setObjectName("LeadsListArea")
        list_area_layout = QVBoxLayout(self.list_area)
        list_area_layout.setContentsMargins(0, 0, 0, 0)
        list_area_layout.setSpacing(0)

        self.list_widget = ListWidget()
        self.list_widget.setObjectName("LeadsList")
        self.list_widget.setSpacing(6)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget.setVerticalScrollMode(ListWidget.ScrollPerPixel)
        list_area_layout.addWidget(self.list_widget, 1)

        self.empty_container = QWidget()
        self.empty_container.setObjectName("LeadsEmptyContainer")
        empty_layout = QVBoxLayout(self.empty_container)
        empty_layout.setContentsMargins(0, 24, 0, 0)
        self.empty_label = BodyLabel("暂无客资记录")
        self.empty_label.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(self.empty_label, 0, Qt.AlignHCenter | Qt.AlignTop)
        empty_layout.addStretch(1)
        list_area_layout.addWidget(self.empty_container, 1)
        self.empty_container.hide()

        root_layout.addWidget(self.list_area, 1)
        
        # Render initial tab
        self._switch_to_claimed()
        self._apply_theme_style()

    def _switch_to_claimed(self):
        self.current_tab = "claimed"
        self._refresh_list()

    def _switch_to_favorite(self):
        self.current_tab = "favorite"
        self._refresh_list()

    def _filter_list(self):
        self._refresh_list()

    def _refresh_list(self):
        keyword = self.search_box.text().strip().lower()
        leads_source = self.claimed_leads if self.current_tab == "claimed" else self.favorite_leads

        filtered_leads = []
        for lead in leads_source:
            unit = lead.get('unit_name', '').lower()
            name = lead.get('customer_name', '').lower()
            phone = lead.get('phone', '').lower()
            region = lead.get('region', '').lower()

            if not keyword or (keyword in unit or keyword in name or keyword in phone or keyword in region):
                filtered_leads.append(lead)

        self.list_widget.setUpdatesEnabled(False)
        try:
            self.list_widget.clear()

            if not filtered_leads:
                self.empty_container.show()
                self.list_widget.hide()
            else:
                self.empty_container.hide()
                self.list_widget.show()

                target_width = max(self.list_widget.viewport().width() - 10, 300)

                for lead in filtered_leads:
                    item = QListWidgetItem(self.list_widget)
                    card = LeadCardWidget(lead, is_claimed=(self.current_tab == "claimed"))
                    card.setFixedWidth(target_width)
                    card.detail_requested.connect(self._open_detail_dialog)
                    card.remove_requested.connect(self._remove_claimed_lead)

                    item.setSizeHint(card.sizeHint())
                    self.list_widget.addItem(item)
                    self.list_widget.setItemWidget(item, card)
        finally:
            self.list_widget.setUpdatesEnabled(True)
        QTimer.singleShot(50, lambda: self.resizeEvent(None))

    def _open_detail_dialog(self, lead_data: dict):
        asyncio.create_task(self._open_detail_dialog_async(lead_data))

    async def _open_detail_dialog_async(self, lead_data: dict):
        from wechat_send_handler import _exec_dialog_async

        dialog = LeadDetailDialog(lead_data, self)
        try:
            await _exec_dialog_async(dialog)
        finally:
            dialog.deleteLater()
        self._refresh_list()

    def _remove_claimed_lead(self, lead_data: dict):
        # Remove from Claimed List
        self.claimed_leads = [x for x in self.claimed_leads if x['id'] != lead_data['id']]
        self._refresh_list()
        
        InfoBar.success(
            title="已移除",
            content=f"已成功将客资「{lead_data.get('unit_name')}」移出认领列表",
            duration=2500,
            position=InfoBarPosition.TOP,
            parent=self.window()
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Handle card width resizing dynamically
        target_width = max(self.list_widget.viewport().width() - 10, 300)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            card = self.list_widget.itemWidget(item)
            if card:
                card.setFixedWidth(target_width)
                item.setSizeHint(card.sizeHint())

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(50, lambda: self.resizeEvent(None))

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        bg_color = "#202020" if is_dark else "#f9f9f9"
        text_main = "#e8e8e8" if is_dark else "#333333"
        text_sub = "#999999" if is_dark else "#888888"
        self.setStyleSheet(f"QFrame#CustomerLeadsPage {{ background-color: {bg_color}; }}")
        self.title_lbl.setStyleSheet(f"color: {text_main}; font-size: 18px; font-weight: bold;")
        self.empty_label.setStyleSheet(f"color: {text_sub}; font-size: 13px;")
