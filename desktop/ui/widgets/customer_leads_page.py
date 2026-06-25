import sys
from PySide6.QtCore import Qt, Signal, QSize, QDateTime, QTimer, QPoint
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidgetItem,
    QDialog, QFrame, QScrollArea, QSizePolicy, QStackedWidget, QListView,
)
from qfluentwidgets import (
    SegmentedWidget, ListWidget, SearchLineEdit,
    PushButton, PrimaryPushButton, TransparentPushButton, TransparentToolButton,
    StrongBodyLabel, BodyLabel, CaptionLabel, SwitchButton,
    isDarkTheme, InfoBar, InfoBarPosition, TextEdit,
    ComboBox, LineEdit, FluentIcon,
)
from ui.confirm_dialog import ask_confirm
from ui.widgets.form_controls import CalendarDateTimePicker, parse_followup_datetime
from utils import mask_phone
from ui.app_fonts import (
    SIZE_MD, WEIGHT_NORMAL, compact_button_qss, label_qss, style_label, text_palette,
)
from ui.widgets import resolve_list_content_width, safe_card_width
from ui.widgets.skeleton import CardListSkeletonPanel
from qfluentwidgets.common.font import getFont

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


LEAD_COLOR_OPTIONS = ("灰色", "蓝色", "绿色", "橙色", "红色")

_LEAD_COLOR_DISPLAY_ALIASES = {
    "null": "灰色",
    "gray": "灰色",
    "grey": "灰色",
    "red": "红色",
    "blue": "蓝色",
    "green": "绿色",
    "orange": "橙色",
    "yellow": "橙色",
}

_LEAD_COLOR_HEX = {
    "灰色": "#8c8c8c",
    "红色": "#ff4d4f",
    "蓝色": "#1890ff",
    "橙色": "#fa8c16",
    "绿色": "#52c41a",
}


def _normalize_lead_color_display(value) -> str:
    s = str(value or "").strip()
    if not s or s.upper() == "NULL":
        return "灰色"
    key = s.lower()
    if key in _LEAD_COLOR_DISPLAY_ALIASES:
        return _LEAD_COLOR_DISPLAY_ALIASES[key]
    if s in LEAD_COLOR_OPTIONS:
        return s
    return "灰色"


class LeadDetailDialog(QDialog):
    """
    客资详细资料与跟进记录对话框 (详情页弹窗)
    高度还原 screenshot 1 风格
    """
    save_requested = Signal(dict)
    remark_add_requested = Signal(dict)
    tel_approve_requested = Signal(int)

    def __init__(self, lead_data: dict, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.lead_data = dict(lead_data or {})
        self._tel_approve_submitted = False
        unit_name = self.lead_data.get('unit_name') or '未知单位'
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
        details_layout.setContentsMargins(15, 10, 15, 4)
        details_layout.setSpacing(8)
        
        # Basic labels
        self.unit_lbl = QLabel(f"单位名称: {lead_data.get('unit_name')}")
        
        # Contact line
        contact_layout = QHBoxLayout()
        contact_layout.setContentsMargins(0, 0, 0, 0)
        contact_layout.setSpacing(5)
        self.phone_mask = mask_phone(lead_data.get('phone', ''))
        self.contact_lbl = QLabel(f"联系人: {lead_data.get('customer_name')} {self.phone_mask}")
        self.view_full_btn = TransparentPushButton("查看完整号码")
        style_label(self.view_full_btn, "link")
        self.view_full_btn.clicked.connect(self._on_view_full_phone_clicked)
        contact_layout.addWidget(self.contact_lbl)
        contact_layout.addWidget(self.view_full_btn)
        contact_layout.addStretch()
        
        self.region_lbl = QLabel(f"地区: {lead_data.get('region')}")
        self.favorite_time_lbl = QLabel(f"收藏时间: {lead_data.get('favorite_time') or '-'}")
        
        remarks_title = StrongBodyLabel("备注")
        
        self.tag_combo = ComboBox()
        self.tag_combo.addItems(["待设置", "20不反感可跟进", "30本月内采购", "40本周内采购", "60选定商品待下单", "80已下单待发货", "停机", "暂停服务", "负责人更换", "拒绝", "未接通"])
        self.tag_combo.setCurrentText(lead_data.get('tags', '待设置'))
        
        self.color_combo = ComboBox()
        self.color_combo.addItems(list(LEAD_COLOR_OPTIONS))
        self.color_combo.setCurrentText(_normalize_lead_color_display(lead_data.get("color")))
        
        self.month_combo = ComboBox()
        self.month_combo.addItems(["待设置"] + [f"{i}月" for i in range(1, 13)])
        month_str = str(lead_data.get("purchase_month") or "").strip()
        if month_str and month_str != "待设置":
            first_month = month_str.replace("，", ",").split(",")[0].strip()
            idx = self.month_combo.findText(first_month)
            self.month_combo.setCurrentIndex(idx if idx >= 0 else 0)
        else:
            self.month_combo.setCurrentIndex(0)
        
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
        remarks_layout.setContentsMargins(12, 10, 12, 6)
        remarks_layout.setSpacing(10)
        
        call_time_lbl = CaptionLabel(f"最近呼叫: {lead_data.get('last_call_time') or '-'}")
        self.call_time_lbl = call_time_lbl
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
        self.confirm_btn.setFixedHeight(32)
        self.confirm_btn.setMinimumWidth(128)
        self.confirm_btn.clicked.connect(self._on_confirm_clicked)
        confirm_row = QHBoxLayout()
        confirm_row.setContentsMargins(0, 0, 0, 0)
        confirm_row.addStretch()
        confirm_row.addWidget(self.confirm_btn)
        confirm_row.addStretch()
        
        details_layout.addWidget(self.unit_lbl)
        details_layout.addLayout(contact_layout)
        details_layout.addWidget(self.region_lbl)
        details_layout.addWidget(self.favorite_time_lbl)
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
        self.scroll_area.setObjectName("FollowupRecordsScroll")
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.verticalScrollBar().setSingleStep(18)
        
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
        style_label(self.empty_lbl, "empty", extra="margin: 30px;")
        
        self._remarks_total = 0
        self._remarks_loading = False
        self.lead_data.setdefault("followup_records", [])
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
        input_layout.setContentsMargins(15, 4, 15, 6)
        input_layout.setSpacing(0)

        self.toggle_followup_btn = TransparentPushButton("＋ 添加跟进内容")
        self.toggle_followup_btn.setFixedHeight(28)
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
        self.note_edit.setObjectName("FollowupNoteEdit")
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
        self.add_note_btn.setMinimumWidth(108)
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

    def position_beside(self, anchor: QWidget):
        """将详情窗贴靠在主列表区域右侧（非模态并排展示）。"""
        if anchor is None:
            return
        top_right = anchor.mapToGlobal(QPoint(anchor.width(), 0))
        x = top_right.x()
        y = top_right.y()
        screen = anchor.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = min(x, avail.right() - self.width())
            x = max(avail.left(), x)
            if y + self.height() > avail.bottom():
                y = max(avail.top(), avail.bottom() - self.height())
        self.move(x, y)

    def load_lead(self, lead_data: dict):
        """切换当前展示的客资（复用同一详情窗）。"""
        self.lead_data = dict(lead_data or {})
        self.lead_data.setdefault("followup_records", [])
        self._tel_approve_submitted = False
        unit_name = self.lead_data.get("unit_name") or "未知单位"
        self.setWindowTitle(f"【{unit_name}】客资详情")

        self.unit_lbl.setText(f"单位名称: {self.lead_data.get('unit_name')}")
        self.phone_mask = mask_phone(self.lead_data.get("phone", ""))
        self.contact_lbl.setText(
            f"联系人: {self.lead_data.get('customer_name')} {self.phone_mask}"
        )
        self.region_lbl.setText(f"地区: {self.lead_data.get('region')}")
        self.favorite_time_lbl.setText(
            f"收藏时间: {self.lead_data.get('favorite_time') or '-'}"
        )
        self.call_time_lbl.setText(
            f"最近呼叫: {self.lead_data.get('last_call_time') or '-'}"
        )

        self.view_full_btn.setEnabled(True)
        self.view_full_btn.setText("查看完整号码")
        self.confirm_btn.setEnabled(True)
        self.confirm_btn.setText("确认")
        self._collapse_followup_input()
        self.note_edit.clear()
        self._update_word_count()

        self.tag_combo.setCurrentText(self.lead_data.get("tags", "待设置"))
        self.color_combo.setCurrentText(
            _normalize_lead_color_display(self.lead_data.get("color"))
        )
        month_str = str(self.lead_data.get("purchase_month") or "").strip()
        if month_str and month_str != "待设置":
            first_month = month_str.replace("，", ",").split(",")[0].strip()
            idx = self.month_combo.findText(first_month)
            self.month_combo.setCurrentIndex(idx if idx >= 0 else 0)
        else:
            self.month_combo.setCurrentIndex(0)

        follow_dt = parse_followup_datetime(self.lead_data.get("followup_time", ""))
        if follow_dt:
            self.followup_picker.datetime = follow_dt
        else:
            self.followup_picker.clear()

        wechat_val = self.lead_data.get("wechat_id", "待设置")
        self.wechat_edit.setText("" if wechat_val == "待设置" else wechat_val)
        budget_val = self.lead_data.get("budget", "待设置")
        self.budget_edit.setText("" if budget_val == "待设置" else budget_val)

        self.fav_switch.blockSignals(True)
        self.fav_switch.setChecked(bool(self.lead_data.get("is_favorite", False)))
        self.fav_switch.blockSignals(False)

        type_val = self.lead_data.get("purchase_type", "待设置")
        self.type_combo.setCurrentText(type_val)

        self._remarks_total = 0
        self._remarks_loading = False
        self._refresh_timeline()
        self._apply_theme_style()

    def _on_view_full_phone_clicked(self):
        if self._tel_approve_submitted:
            return
        lead_id = self.lead_data.get("id")
        if lead_id is None:
            InfoBar.warning(
                title="无法申请",
                content="缺少客资 ID",
                duration=2500,
                position=InfoBarPosition.TOP,
                parent=self.parentWidget() or self,
            )
            return
        unit = self.lead_data.get("unit_name") or "该客资"
        if not ask_confirm(
            self,
            "申请查看完整号码",
            f"将向米城系统提交查看「{unit}」完整联系电话的申请。\n"
            "审批通过后请通过企业微信获取完整号码，本客户端不会直接展示完整号码。\n\n"
            "是否确认提交申请？",
        ):
            return
        self.view_full_btn.setEnabled(False)
        self.view_full_btn.setText("提交中...")
        self.tel_approve_requested.emit(int(lead_id))

    def handle_tel_approve_result(self, ok: bool, message: str = ""):
        if ok:
            self._tel_approve_submitted = True
            self.view_full_btn.setText("已提交申请")
            self.view_full_btn.setEnabled(False)
            InfoBar.success(
                title="申请已提交",
                content="查看完整号码的申请已提交，请等待审批后通过外部方式查看",
                duration=3500,
                position=InfoBarPosition.TOP,
                parent=self.parentWidget() or self,
            )
            return
        self.view_full_btn.setEnabled(True)
        self.view_full_btn.setText("查看完整号码")
        InfoBar.warning(
            title="申请失败",
            content=message or "请稍后重试",
            duration=3500,
            position=InfoBarPosition.TOP,
            parent=self.parentWidget() or self,
        )

    def _toggle_favorite(self, checked):
        self.lead_data['is_favorite'] = checked

    def _on_confirm_clicked(self):
        self.confirm_btn.setEnabled(False)
        self.confirm_btn.setText("保存中...")
        self.save_requested.emit(self._build_save_payload())

    def handle_save_result(self, ok: bool, message: str = ""):
        if ok:
            self._apply_form_to_lead_data()
            InfoBar.success(
                title="保存成功",
                content="客资信息已同步至主系统",
                duration=2000,
                position=InfoBarPosition.TOP,
                parent=self.parentWidget() or self,
            )
            self.confirm_btn.setEnabled(True)
            self.confirm_btn.setText("确认")
            return
        self.confirm_btn.setEnabled(True)
        self.confirm_btn.setText("确认")
        InfoBar.warning(
            title="保存失败",
            content=message or "请稍后重试",
            duration=3500,
            position=InfoBarPosition.TOP,
            parent=self.parentWidget() or self,
        )

    def _build_save_payload(self) -> dict:
        follow_dt = self.followup_picker.datetime
        followup_time = (
            follow_dt.toString("yyyy-MM-dd HH:mm:ss") if follow_dt.isValid() else "待设置"
        )
        return {
            "lead_id": self.lead_data.get("id"),
            "info": {
                "tags": self.tag_combo.currentText(),
                "color": self.color_combo.currentText(),
                "purchase_month": self.month_combo.currentText(),
                "followup_time": followup_time,
                "wechat_id": self.wechat_edit.text().strip() or "待设置",
                "budget": self.budget_edit.text().strip() or "待设置",
                "purchase_type": self.type_combo.currentText(),
                "is_favorite": self.fav_switch.isChecked(),
            },
        }

    def _apply_form_to_lead_data(self):
        payload = self._build_save_payload()
        info = payload.get("info") or {}
        self.lead_data["tags"] = info.get("tags")
        self.lead_data["color"] = info.get("color")
        self.lead_data["purchase_month"] = info.get("purchase_month")
        self.lead_data["followup_time"] = info.get("followup_time")
        self.lead_data["wechat_id"] = info.get("wechat_id")
        self.lead_data["budget"] = info.get("budget")
        self.lead_data["purchase_type"] = info.get("purchase_type")
        self.lead_data["is_favorite"] = bool(info.get("is_favorite"))

    def _save_attributes(self):
        self._apply_form_to_lead_data()

    def closeEvent(self, event):
        super().closeEvent(event)

    def close(self):
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
        if not text or len(text) > 500:
            return
        lead_id = self.lead_data.get("id")
        if lead_id is None:
            InfoBar.warning(
                title="无法提交",
                content="缺少客资 ID",
                duration=2500,
                position=InfoBarPosition.TOP,
                parent=self.parentWidget() or self,
            )
            return
        self.add_note_btn.setEnabled(False)
        self.add_note_btn.setText("提交中...")
        self.remark_add_requested.emit({"lead_id": int(lead_id), "remark": text})

    def handle_remark_add_result(self, ok: bool, message: str = "", data: dict | None = None):
        if not self._timeline_ui_alive():
            return
        self.add_note_btn.setText("立即添加")
        if not ok:
            self._update_word_count()
            InfoBar.warning(
                title="添加失败",
                content=message or "请稍后重试",
                duration=3500,
                position=InfoBarPosition.TOP,
                parent=self.parentWidget() or self,
            )
            return
        row = data or {}
        new_record = {
            "id": row.get("id"),
            "time": str(row.get("create_time") or "-"),
            "content": str(row.get("remark") or self.note_edit.toPlainText().strip()),
        }
        self.lead_data.setdefault("followup_records", []).insert(0, new_record)
        self._remarks_total = max(self._remarks_total + 1, len(self.lead_data["followup_records"]))
        self.note_edit.clear()
        self._update_word_count()
        self._refresh_timeline()
        self._collapse_followup_input()
        InfoBar.success(
            title="添加成功",
            content="跟进记录已同步至主系统",
            duration=2000,
            position=InfoBarPosition.TOP,
            parent=self.parentWidget() or self,
        )

    def _timeline_ui_alive(self) -> bool:
        try:
            self.empty_lbl.setText(self.empty_lbl.text())
            return True
        except RuntimeError:
            return False

    def _clear_timeline_cards(self):
        """移除时间轴卡片；empty_lbl 只从布局摘下，不 deleteLater。"""
        i = 0
        while i < self.timeline_layout.count() - 1:
            item = self.timeline_layout.itemAt(i)
            if item is None:
                i += 1
                continue
            widget = item.widget()
            if widget is self.empty_lbl:
                self.empty_lbl.hide()
                self.timeline_layout.takeAt(i)
                continue
            self.timeline_layout.takeAt(i)
            if widget is not None:
                widget.deleteLater()

    def show_remarks_loading(self):
        if not self._timeline_ui_alive():
            return
        self._remarks_loading = True
        self.lead_data["followup_records"] = []
        self._clear_timeline_cards()
        self.empty_lbl.setText("正在加载跟进记录...")
        self.timeline_layout.insertWidget(0, self.empty_lbl)
        self.empty_lbl.show()

    @staticmethod
    def _remark_text_from_row(row: dict) -> str:
        for key in ("remark", "remarks", "content", "log"):
            val = row.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
        return ""

    def set_remarks_page(self, data: dict | None):
        if not self._timeline_ui_alive():
            return
        self._remarks_loading = False
        data = data or {}
        self._remarks_total = int(data.get("total") or 0)
        rows = list(data.get("list") or data.get("remarks") or [])
        records = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            records.append(
                {
                    "id": row.get("id"),
                    "time": str(row.get("create_time") or row.get("time") or "-"),
                    "content": self._remark_text_from_row(row),
                }
            )
        self.lead_data["followup_records"] = records
        self._refresh_timeline()

    def show_remarks_error(self, message: str = ""):
        if not self._timeline_ui_alive():
            return
        self._remarks_loading = False
        self.lead_data["followup_records"] = []
        self._clear_timeline_cards()
        self.empty_lbl.setText(message or "加载跟进记录失败")
        self.timeline_layout.insertWidget(0, self.empty_lbl)
        self.empty_lbl.show()

    def _refresh_timeline(self):
        if not self._timeline_ui_alive():
            return
        self._clear_timeline_cards()

        records = self.lead_data.get("followup_records", [])
        if not records:
            if not self._remarks_loading:
                self.empty_lbl.setText("还没有任何跟进记录")
            self.timeline_layout.insertWidget(0, self.empty_lbl)
            self.empty_lbl.show()
        else:
            self.empty_lbl.hide()
            for record in reversed(records):
                card = QFrame()
                card.setObjectName("TimelineCard")
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(10, 8, 10, 8)
                card_layout.setSpacing(4)
                
                pal = text_palette()
                time_lbl = QLabel(
                    str(record.get("time") or record.get("create_time") or "-")
                )
                style_label(time_lbl, "caption_emphasis", color=pal.accent)
                content_lbl = QLabel(
                    str(
                        record.get("content")
                        or record.get("remark")
                        or record.get("remarks")
                        or ""
                    )
                )
                content_lbl.setWordWrap(True)
                style_label(content_lbl, "body", extra="line-height: 1.5;")
                
                card_layout.addWidget(time_lbl)
                card_layout.addWidget(content_lbl)
                self.timeline_layout.insertWidget(0, card)
                
    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        pal = text_palette()
        bg_color = "#272727" if is_dark else "#fdfdfd"
        card_bg = "#303030" if is_dark else "#f5f5f5"
        text_color = pal.primary
        border_color = "rgba(255,255,255,0.08)" if is_dark else "rgba(0,0,0,0.08)"
        input_bg = "#333333" if is_dark else "#ffffff"
        input_border = "#404040" if is_dark else "#d0d0d0"
        scroll_handle = "rgba(255, 255, 255, 0.25)" if is_dark else "rgba(0, 0, 0, 0.18)"
        scroll_handle_hover = "rgba(255, 255, 255, 0.35)" if is_dark else "rgba(0, 0, 0, 0.28)"
        placeholder = pal.muted
        
        if hasattr(self, 'month_combo') and hasattr(self.month_combo, '_apply_theme_style'):
            self.month_combo._apply_theme_style()

        note_font = getFont(SIZE_MD, QFont.Weight.Normal)
        self.note_edit.setFont(note_font)
        style_label(self.count_lbl, "caption")
            
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {bg_color};
            }}
            QFrame#DetailsContainer {{
                background-color: {bg_color};
            }}
            QFrame#DetailsContainer QLabel {{
                color: {text_color};
                font-size: 12px;
            }}
            QFrame#RemarksCard {{
                background-color: {card_bg};
                border: 1px solid {border_color};
                border-radius: 8px;
            }}
            QScrollArea#FollowupRecordsScroll {{
                border: none;
                background: transparent;
            }}
            QScrollArea#FollowupRecordsScroll QWidget#qt_scrollarea_viewport,
            QWidget#TimelineContent {{
                background: transparent;
            }}
            QScrollArea#FollowupRecordsScroll QScrollBar:vertical {{
                background: transparent;
                width: 6px;
                margin: 2px 2px 2px 0px;
            }}
            QScrollArea#FollowupRecordsScroll QScrollBar::handle:vertical {{
                background: {scroll_handle};
                border-radius: 3px;
                min-height: 28px;
            }}
            QScrollArea#FollowupRecordsScroll QScrollBar::handle:vertical:hover {{
                background: {scroll_handle_hover};
            }}
            QScrollArea#FollowupRecordsScroll QScrollBar::add-line:vertical,
            QScrollArea#FollowupRecordsScroll QScrollBar::sub-line:vertical {{
                height: 0px;
                border: none;
                background: transparent;
            }}
            QScrollArea#FollowupRecordsScroll QScrollBar::add-page:vertical,
            QScrollArea#FollowupRecordsScroll QScrollBar::sub-page:vertical {{
                background: transparent;
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
            TextEdit#FollowupNoteEdit {{
                background-color: {input_bg};
                border: 1px solid {input_border};
                border-radius: 6px;
                padding: 8px;
                color: {text_color};
                font-size: {SIZE_MD}px;
                font-weight: {WEIGHT_NORMAL};
                selection-background-color: rgba(7, 193, 96, 0.35);
            }}
            TextEdit#FollowupNoteEdit:focus {{
                border: 1px solid rgba(7, 193, 96, 0.65);
            }}
        """)
        self.note_edit.setStyleSheet(
            f"TextEdit#FollowupNoteEdit::placeholder {{ color: {placeholder}; }}"
        )


def _format_followup_display(value) -> str:
    text = str(value or "").strip()
    if not text or text in ("待设置", "设置"):
        return "待设置"
    return text


class LeadCardWidget(QFrame):
    """
    客资卡片：以卡片形式展示客资基础信息，点击可进入详情
    """
    detail_requested = Signal(dict)
    remove_requested = Signal(dict)
    changhu_call_requested = Signal(dict, str)
    yunke_call_requested = Signal(dict)

    def __init__(self, lead_data: dict, is_claimed: bool, parent=None):
        super().__init__(parent)
        self.lead_data = lead_data
        self.is_claimed = is_claimed
        self.setObjectName("LeadCard")
        self.setFrameShape(QFrame.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        
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
            self.extra_lbl.setWordWrap(True)
            root.addWidget(self.extra_lbl)
            
        # 4. Time Information Row
        self.time_lbl = CaptionLabel()
        self.time_lbl.setWordWrap(True)
        if self.is_claimed:
            self.time_lbl.setText(f"分配时间: {lead_data.get('allocation_time', '-')}    回收倒计时: {lead_data.get('recycle_days', '390天')}")
            self.time_lbl.setStyleSheet("color: #ff4d4f; font-weight: bold;")
        else:
            self.time_lbl.setText(
                f"回访时间: {_format_followup_display(lead_data.get('followup_time'))}"
            )
            self.time_lbl.setStyleSheet("color: #722ed1; font-weight: bold;")
        root.addWidget(self.time_lbl)
            
        # 5. Footer Buttons Row
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 2, 0, 0)
        footer_layout.setSpacing(8)
        footer_layout.addStretch(1)

        self.call1_btn = PushButton("畅呼外呼")
        self.call1_btn.setFixedHeight(24)
        self.call1_btn.setToolTip("通过畅呼系统拨打外呼电话")
        self.call1_btn.clicked.connect(self._on_call1_clicked)

        self.call2_btn = PushButton("云客外呼")
        self.call2_btn.setFixedHeight(24)
        self.call2_btn.setToolTip("通过云客系统拨打外呼电话")
        self.call2_btn.clicked.connect(self._on_call2_clicked)

        self.detail_btn = PushButton("详情")
        self.detail_btn.setFixedHeight(24)
        self.detail_btn.clicked.connect(self._on_detail_clicked)

        footer_layout.addWidget(self.call1_btn)
        footer_layout.addWidget(self.call2_btn)
        footer_layout.addWidget(self.detail_btn)

        if self.is_claimed:
            self.remove_btn = PushButton("移除")
            self.remove_btn.setFixedHeight(24)
            self.remove_btn.setStyleSheet(
                "QPushButton { color: #ff4d4f; } QPushButton:hover { border-color: #ff4d4f; }"
            )
            self.remove_btn.clicked.connect(self._on_remove_clicked)
            footer_layout.addWidget(self.remove_btn)

        root.addLayout(footer_layout)
        self._apply_theme_style()

    def refresh_from_data(self, lead_data: dict):
        """就地刷新卡片展示，避免整表重绘。"""
        self.lead_data = lead_data
        self.unit_lbl.setText(lead_data.get("unit_name", "未知单位"))
        masked_phone = mask_phone(lead_data.get("phone", ""))
        self.info_lbl.setText(
            f"联系人: {lead_data.get('customer_name')} | {masked_phone}   地区: {lead_data.get('region')}"
        )
        extra_parts = []
        tags = lead_data.get("tags")
        if tags and tags != "待设置":
            extra_parts.append(f"标签: {tags}")
        budget = lead_data.get("budget")
        if budget and budget != "待设置":
            extra_parts.append(f"预算: {budget}")
        last_call = lead_data.get("last_call_time")
        if last_call and last_call != "-":
            extra_parts.append(f"最近呼叫: {last_call}")
        if extra_parts:
            if not hasattr(self, "extra_lbl"):
                self.extra_lbl = CaptionLabel()
                self.extra_lbl.setWordWrap(True)
                layout = self.layout()
                if layout and layout.count() >= 2:
                    layout.insertWidget(2, self.extra_lbl)
            self.extra_lbl.setText("  ·  ".join(extra_parts))
            self.extra_lbl.show()
        elif hasattr(self, "extra_lbl"):
            self.extra_lbl.hide()
        self._apply_theme_style()
        self.updateGeometry()
        if self.is_claimed:
            self.time_lbl.setText(
                f"分配时间: {lead_data.get('allocation_time', '-')}    "
                f"回收倒计时: {lead_data.get('recycle_days', '390天')}"
            )
        else:
            self.time_lbl.setText(
                f"回访时间: {_format_followup_display(lead_data.get('followup_time'))}"
            )

    def _on_call1_clicked(self):
        from ui.changhu_phone_picker import pick_changhu_tel, resolve_changhu_phones

        if not resolve_changhu_phones(self):
            InfoBar.warning(
                title="畅呼外呼失败",
                content="未配置畅呼号码，请在米城账号中绑定畅呼手机号后重试",
                duration=3500,
                position=InfoBarPosition.TOP,
                parent=self.window(),
            )
            return
        phones = resolve_changhu_phones(self)
        changhu_tel = pick_changhu_tel(self)
        if not changhu_tel:
            return
        if len(phones) > 1:
            unit = self.lead_data.get("unit_name") or "该客资"
            masked = mask_phone(self.lead_data.get("phone", ""))
            phone_hint = f"（{masked}）" if masked else ""
            if not ask_confirm(
                self,
                "畅呼外呼",
                f"确认使用畅呼号码 {changhu_tel} 拨打「{unit}」{phone_hint}？",
            ):
                return
        self.call1_btn.setEnabled(False)
        self.call1_btn.setText("外呼中...")
        self.changhu_call_requested.emit(self.lead_data, changhu_tel)

    def handle_changhu_call_result(self, ok: bool, message: str = ""):
        self.call1_btn.setEnabled(True)
        self.call1_btn.setText("畅呼外呼")
        if ok:
            from datetime import datetime

            self.lead_data["last_call_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            InfoBar.success(
                title="畅呼外呼",
                content=message or f"已发起外呼，拨打 {mask_phone(self.lead_data.get('phone', ''))}",
                duration=2500,
                position=InfoBarPosition.TOP,
                parent=self.window(),
            )
            return
        if message:
            InfoBar.warning(
                title="畅呼外呼失败",
                content=message,
                duration=3500,
                position=InfoBarPosition.TOP,
                parent=self.window(),
            )

    def _on_call2_clicked(self):
        self.call2_btn.setEnabled(False)
        self.call2_btn.setText("外呼中...")
        self.yunke_call_requested.emit(self.lead_data)

    def handle_yunke_call_result(self, ok: bool, message: str = ""):
        self.call2_btn.setEnabled(True)
        self.call2_btn.setText("云客外呼")
        if ok:
            from datetime import datetime

            self.lead_data["last_call_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            InfoBar.success(
                title="云客外呼",
                content=message or f"已发起外呼，拨打 {mask_phone(self.lead_data.get('phone', ''))}",
                duration=2500,
                position=InfoBarPosition.TOP,
                parent=self.window(),
            )
            return
        InfoBar.warning(
            title="云客外呼失败",
            content=message or "请稍后重试",
            duration=3500,
            position=InfoBarPosition.TOP,
            parent=self.window(),
        )

    def _on_detail_clicked(self):
        self.detail_requested.emit(self.lead_data)

    def _on_remove_clicked(self):
        unit = self.lead_data.get("unit_name") or "该客资"
        if not ask_confirm(
            self,
            "移除客资",
            f"确认移除「{unit}」？移除后 7 天内即使再次分配也不会出现在待拨打列表中。",
        ):
            return
        if hasattr(self, "remove_btn"):
            self.remove_btn.setEnabled(False)
            self.remove_btn.setText("移除中...")
        self.remove_requested.emit(self.lead_data)

    def handle_ignore_result(self, ok: bool, message: str = ""):
        if hasattr(self, "remove_btn"):
            self.remove_btn.setEnabled(True)
            self.remove_btn.setText("移除")
        if ok:
            return
        InfoBar.warning(
            title="移除失败",
            content=message or "请稍后重试",
            duration=3500,
            position=InfoBarPosition.TOP,
            parent=self.window(),
        )
        
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
        # Map color names to actual hex codes
        color_name = _normalize_lead_color_display(self.lead_data.get("color"))
        side_color = _LEAD_COLOR_HEX.get(color_name, "#8c8c8c")
        
        self.setStyleSheet(f"""
            QFrame#LeadCard {{
                background-color: {card_bg};
                border: 1px solid {card_border};
                border-left: 4px solid {side_color};
                border-radius: 8px;
            }}
        """)
        pal = text_palette()
        text_main = pal.primary
        text_sub = pal.secondary
        style_label(self.unit_lbl, "body_emphasis", color=text_main)
        style_label(self.info_lbl, "caption", color=text_sub)
        if hasattr(self, 'extra_lbl'):
            style_label(self.extra_lbl, "caption", color=text_sub)

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
        btn_style = compact_button_qss(
            fg=btn_fg, bg=btn_bg, border=btn_border,
            hover_bg=btn_hover, hover_border="#07c160",
        ).replace("border-radius: 5px", "border-radius: 4px")
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
    claimed_leads_fetch_requested = Signal(int, int, bool, bool, int)  # page, page_size, append, silent, seq
    favorite_leads_fetch_requested = Signal(
        int, int, bool, bool, str, int
    )  # page, page_size, append, silent, client_name, seq
    lead_update_requested = Signal(dict)
    lead_remarks_fetch_requested = Signal(int, int, int)  # lead_id, page, page_size
    lead_remark_add_requested = Signal(dict)
    lead_tel_approve_requested = Signal(int)
    lead_ignore_requested = Signal(int)
    lead_changhu_call_requested = Signal(int, str)
    lead_yunke_call_requested = Signal(int)

    LEADS_AUTO_REFRESH_MS = 90_000
    LEADS_PAGE_SIZE = 50
    CLAIMED_FETCH_PAGE_SIZE = 100
    CLAIMED_DISPLAY_PAGE_SIZE = 50
    LEADS_SCROLL_SINGLE_STEP = 20
    LEADS_SCROLL_PAGE_STEP = 72

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CustomerLeadsPage")
        self.setFrameShape(QFrame.NoFrame)

        self.claimed_leads: list[dict] = []
        self.favorite_leads: list[dict] = []
        self.claimed_total = 0
        self.favorite_total = 0
        self.claimed_page = 1
        self.favorite_page = 1
        self.claimed_page_size = self.LEADS_PAGE_SIZE
        self.favorite_page_size = self.LEADS_PAGE_SIZE
        self._mibuddy_bound = False
        self._leads_loading = False
        self._active_detail_dialog = None
        self._detail_dialog: LeadDetailDialog | None = None
        self._outbound_call_cards: dict[int, LeadCardWidget] = {}
        self._detail_list_patched = False
        self._claimed_cache_valid = False
        self._favorite_cache_valid = False
        self._favorite_client_name = ""
        self._favorite_cached_client_name = ""
        self._awaiting_binding_for_load = False
        self._leads_module_entered_once = False
        self._claimed_fetch_seq = 0
        self._favorite_fetch_seq = 0
        self._claimed_display_page = 1
        self._favorite_highest_page = 0
        self._favorite_has_more = False
        self._favorite_loading_more = False
        self._rendered_fingerprints: dict[str, tuple] = {}
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(self.LEADS_AUTO_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._on_auto_refresh_tick)
        self._favorite_search_timer = QTimer(self)
        self._favorite_search_timer.setSingleShot(True)
        self._favorite_search_timer.setInterval(400)
        self._favorite_search_timer.timeout.connect(self._on_favorite_search_debounced)

        self.current_tab = "claimed"  # "claimed" or "favorite"
        
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
        self.btn_leads_refresh = TransparentToolButton(FluentIcon.SYNC, self)
        self.btn_leads_refresh.setToolTip("刷新当前列表")
        self.btn_leads_refresh.setFixedSize(32, 32)
        title_layout.addWidget(self.btn_leads_refresh)
        header_layout.addLayout(title_layout)

        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(15)

        self.segmented_tab = SegmentedWidget()
        self.segmented_tab.setMinimumWidth(200)
        self.segmented_tab.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.segmented_tab.addItem("claimed", "认领客资", self._switch_to_claimed)
        self.segmented_tab.addItem("favorite", "收藏客资", self._switch_to_favorite)

        self.search_box = SearchLineEdit()
        self.search_box.setPlaceholderText("搜索单位、地区、电话或姓名...")
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

        self.claimed_list_widget = self._make_leads_list_widget("LeadsListClaimed")
        self.favorite_list_widget = self._make_leads_list_widget("LeadsListFavorite")
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.claimed_list_widget)
        self.list_stack.addWidget(self.favorite_list_widget)
        list_area_layout.addWidget(self.list_stack, 1)

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

        self._leads_loading_overlay = QWidget(self.list_area)
        self._leads_loading_overlay.setObjectName("LeadsListLoadingOverlay")
        leads_overlay_layout = QVBoxLayout(self._leads_loading_overlay)
        leads_overlay_layout.setContentsMargins(0, 0, 0, 0)
        self._leads_loading_skeleton = CardListSkeletonPanel(
            card_style="lead",
            row_count=5,
            row_spacing=8,
            margins=(4, 12, 4, 8),
            parent=self._leads_loading_overlay,
        )
        leads_overlay_layout.addWidget(self._leads_loading_skeleton, 1)
        self._leads_loading_overlay.hide()

        self.claimed_pagination_bar = QWidget()
        self.claimed_pagination_bar.setObjectName("ClaimedPaginationBar")
        claimed_pagination_layout = QHBoxLayout(self.claimed_pagination_bar)
        claimed_pagination_layout.setContentsMargins(0, 4, 0, 0)
        claimed_pagination_layout.setSpacing(12)
        claimed_pagination_layout.addStretch()
        self.claimed_page_prev_btn = TransparentPushButton("上一页")
        self.claimed_page_prev_btn.setFixedHeight(32)
        self.claimed_page_prev_btn.clicked.connect(self._on_claimed_page_prev)
        self.claimed_page_info = CaptionLabel("")
        self.claimed_page_next_btn = TransparentPushButton("下一页")
        self.claimed_page_next_btn.setFixedHeight(32)
        self.claimed_page_next_btn.clicked.connect(self._on_claimed_page_next)
        claimed_pagination_layout.addWidget(self.claimed_page_prev_btn)
        claimed_pagination_layout.addWidget(self.claimed_page_info)
        claimed_pagination_layout.addWidget(self.claimed_page_next_btn)
        claimed_pagination_layout.addStretch()
        self.claimed_pagination_bar.hide()
        list_area_layout.addWidget(self.claimed_pagination_bar, 0, Qt.AlignHCenter)

        self.load_more_btn = TransparentPushButton("加载更多")
        self.load_more_btn.hide()
        self.load_more_btn.clicked.connect(self._on_load_more_clicked)
        list_area_layout.addWidget(self.load_more_btn, 0, Qt.AlignHCenter)

        root_layout.addWidget(self.list_area, 1)
        
        # Render initial tab
        self._switch_to_claimed()
        self._apply_theme_style()

        self.btn_leads_refresh.clicked.connect(self._on_leads_refresh_clicked)

    @property
    def list_widget(self) -> ListWidget:
        """当前标签对应的列表（兼容外部主题/布局刷新）。"""
        return self._list_widget_for(self.current_tab)

    def iter_leads_list_widgets(self):
        yield self.claimed_list_widget
        yield self.favorite_list_widget

    def _make_leads_list_widget(self, object_name: str) -> ListWidget:
        lw = ListWidget()
        lw.setObjectName(object_name)
        lw.setSpacing(6)
        lw.setResizeMode(QListView.ResizeMode.Adjust)
        lw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lw.setVerticalScrollMode(ListWidget.ScrollPerPixel)
        vbar = lw.verticalScrollBar()
        vbar.setSingleStep(self.LEADS_SCROLL_SINGLE_STEP)
        vbar.setPageStep(self.LEADS_SCROLL_PAGE_STEP)
        return lw

    def _lead_card_target_width(self, list_widget: ListWidget) -> int:
        """卡片宽度不超过列表视口，避免撑出窗口总宽度导致右侧按钮被遮挡。"""
        vp = resolve_list_content_width(list_widget)
        if vp <= 0:
            sibling = (
                self.claimed_list_widget
                if list_widget is self.favorite_list_widget
                else self.favorite_list_widget
            )
            vp = resolve_list_content_width(sibling)
        if vp <= 0 and hasattr(self, "list_area"):
            vp = self.list_area.width()
        return safe_card_width(list_widget, viewport_width=vp or None)

    def _sync_lead_card_item_geometry(
        self, item: QListWidgetItem, card: LeadCardWidget, target_w: int
    ):
        """同步单张卡片宽度与列表项高度（需在内容变更后调用 adjustSize）。"""
        card.setMinimumWidth(0)
        card.setFixedWidth(target_w)
        card.setMaximumWidth(target_w)
        if card.layout():
            card.layout().activate()
        card.adjustSize()
        hint_h = max(card.sizeHint().height(), card.minimumSizeHint().height(), card.height())
        item.setSizeHint(QSize(target_w, hint_h))

    def _sync_lead_card_widths(self, list_widget: ListWidget | None = None):
        targets = [list_widget] if list_widget is not None else list(self.iter_leads_list_widgets())
        for lw in targets:
            target_w = self._lead_card_target_width(lw)
            if target_w <= 0:
                continue
            for i in range(lw.count()):
                item = lw.item(i)
                card = lw.itemWidget(item)
                if card is None:
                    continue
                self._sync_lead_card_item_geometry(item, card, target_w)
            lw.doItemsLayout()
            lw.viewport().update()

    def _list_widget_for(self, tab: str | None = None) -> ListWidget:
        tab = tab or self.current_tab
        return self.claimed_list_widget if tab == "claimed" else self.favorite_list_widget

    def _list_stack_index_for(self, tab: str) -> int:
        return 0 if tab == "claimed" else 1

    def _show_list_stack_for(self, tab: str | None = None):
        tab = tab or self.current_tab
        self.list_stack.setCurrentIndex(self._list_stack_index_for(tab))

    def _on_leads_refresh_clicked(self):
        if not self._mibuddy_bound:
            return
        self.invalidate_leads_cache(self.current_tab)
        self.ensure_current_tab_loaded(force=True)

    def _emit_claimed_leads_fetch(self, page: int, page_size: int, append: bool, silent: bool):
        self._claimed_fetch_seq += 1
        self.claimed_leads_fetch_requested.emit(
            1, self.CLAIMED_FETCH_PAGE_SIZE, False, silent, self._claimed_fetch_seq
        )

    def _emit_favorite_leads_fetch(
        self, page: int, page_size: int, append: bool, silent: bool, client_name: str = ""
    ):
        self._favorite_fetch_seq += 1
        self.favorite_leads_fetch_requested.emit(
            page, page_size, append, silent, client_name, self._favorite_fetch_seq
        )

    @staticmethod
    def _merge_head_page(current: list[dict], head_items: list[dict]) -> list[dict]:
        if not head_items:
            return current
        head_ids = {x.get("id") for x in head_items}
        tail = [x for x in current if x.get("id") not in head_ids]
        return list(head_items) + tail

    @staticmethod
    def _calc_total_pages(total: int, page_size: int) -> int:
        total = max(0, int(total or 0))
        size = max(1, int(page_size or CustomerLeadsWidget.LEADS_PAGE_SIZE))
        if total <= 0:
            return 0
        return (total + size - 1) // size

    def _claimed_filtered_display_pages(self, filtered_count: int) -> int:
        if filtered_count <= 0:
            return 0
        size = max(1, self.CLAIMED_DISPLAY_PAGE_SIZE)
        return (filtered_count + size - 1) // size

    def _slice_claimed_display_page(self, filtered_leads: list[dict]) -> list[dict]:
        total_pages = self._claimed_filtered_display_pages(len(filtered_leads))
        if total_pages <= 0:
            return []
        page = max(1, min(self._claimed_display_page, total_pages))
        if page != self._claimed_display_page:
            self._claimed_display_page = page
        size = self.CLAIMED_DISPLAY_PAGE_SIZE
        start = (page - 1) * size
        return filtered_leads[start : start + size]

    def _sync_claimed_pagination_chrome(self, filtered_count: int):
        if self.current_tab != "claimed" or self._leads_loading:
            self.claimed_pagination_bar.hide()
            return
        total_pages = self._claimed_filtered_display_pages(filtered_count)
        if total_pages <= 1:
            self.claimed_pagination_bar.hide()
            return
        page = max(1, min(self._claimed_display_page, total_pages))
        self._claimed_display_page = page
        self.claimed_page_info.setText(
            f"第 {page} / {total_pages} 页（共 {filtered_count} 条）"
        )
        self.claimed_page_prev_btn.setEnabled(page > 1)
        self.claimed_page_next_btn.setEnabled(page < total_pages)
        self.claimed_pagination_bar.show()

    def _on_claimed_page_prev(self):
        if self._claimed_display_page <= 1:
            return
        self._claimed_display_page -= 1
        self._rendered_fingerprints.pop("claimed", None)
        self._refresh_tab_list("claimed")
        lw = self.claimed_list_widget
        lw.verticalScrollBar().setValue(0)

    def _on_claimed_page_next(self):
        filtered, _, _ = self._filtered_leads_for_tab("claimed")
        total_pages = self._claimed_filtered_display_pages(len(filtered))
        if self._claimed_display_page >= total_pages:
            return
        self._claimed_display_page += 1
        self._rendered_fingerprints.pop("claimed", None)
        self._refresh_tab_list("claimed")
        lw = self.claimed_list_widget
        lw.verticalScrollBar().setValue(0)

    def _has_more_favorite(self) -> bool:
        return self.favorite_total > 0 and len(self.favorite_leads) < self.favorite_total

    def append_favorite_leads_batch(
        self, data: dict | None, *, client_name: str = "", seq: int = 0
    ) -> int:
        keyword = (client_name or self._favorite_client_name) or ""
        if keyword != self._favorite_client_name:
            return 0
        if seq and seq != self._favorite_fetch_seq:
            return 0
        data = data or {}
        items = list(data.get("list") or [])
        page = int(data.get("page") or 1)
        self.favorite_total = int(data.get("total") or self.favorite_total)
        self.favorite_page = page
        self.favorite_page_size = int(data.get("page_size") or self.favorite_page_size)
        seen = {x.get("id") for x in self.favorite_leads}
        added = 0
        for row in items:
            rid = row.get("id")
            if rid is not None and rid not in seen:
                self.favorite_leads.append(row)
                seen.add(rid)
                added += 1
        self._favorite_highest_page = max(self._favorite_highest_page, page)
        self._favorite_cache_valid = True
        self._favorite_cached_client_name = keyword
        return added

    def finalize_favorite_list(self, *, preserve_scroll: bool = False):
        self._rendered_fingerprints.pop("favorite", None)
        self._refresh_tab_list("favorite", preserve_scroll=preserve_scroll)

    def on_page_activated(self):
        """进入客资列表模块时：首次自动加载认领列表，并等待绑定状态同步。"""
        first_enter = not self._leads_module_entered_once
        self._awaiting_binding_for_load = True
        if first_enter:
            self._leads_module_entered_once = True
            self.current_tab = "claimed"
            self.search_box.setPlaceholderText("搜索单位、地区、电话或姓名...")
        if self._mibuddy_bound:
            self._awaiting_binding_for_load = False
            self.start_auto_refresh()
            if first_enter:
                self._load_claimed_leads(force=True)
            else:
                self.ensure_current_tab_loaded()
        else:
            self._refresh_list()

    def on_binding_synced(self):
        """设置页绑定状态刷新完成后，若正在客资页则触发首次加载。"""
        if not self._awaiting_binding_for_load:
            return
        self._awaiting_binding_for_load = False
        if not self._mibuddy_bound:
            self._refresh_list()
            return
        self.start_auto_refresh()
        self._load_claimed_leads(force=True)

    def start_auto_refresh(self):
        if self._mibuddy_bound:
            self._refresh_timer.start()

    def stop_auto_refresh(self):
        self._refresh_timer.stop()

    def _on_auto_refresh_tick(self):
        """停留客资页时静默同步两端列表（不遮挡当前界面）。"""
        if not self._mibuddy_bound or self._leads_loading:
            return
        self._emit_claimed_leads_fetch(1, self.CLAIMED_FETCH_PAGE_SIZE, False, True)
        self._emit_favorite_leads_fetch(
            1, self.favorite_page_size, False, True, self._favorite_client_name
        )

    def _request_background_sync(self, tab: str | None = None):
        if not self._mibuddy_bound:
            return
        tabs = ("claimed", "favorite") if tab is None else (tab,)
        if "claimed" in tabs:
            self._emit_claimed_leads_fetch(1, self.CLAIMED_FETCH_PAGE_SIZE, False, True)
        if "favorite" in tabs:
            self._emit_favorite_leads_fetch(
                1, self.favorite_page_size, False, True, self._favorite_client_name
            )

    def invalidate_leads_cache(self, tab: str | None = None):
        if tab in (None, "claimed"):
            self._claimed_cache_valid = False
            self._claimed_display_page = 1
            self._rendered_fingerprints.pop("claimed", None)
        if tab in (None, "favorite"):
            self._favorite_cache_valid = False
            self._favorite_highest_page = 0
            self._favorite_has_more = False
            self._favorite_cached_client_name = ""
            self._rendered_fingerprints.pop("favorite", None)

    def ensure_current_tab_loaded(self, *, force: bool = False):
        if self.current_tab == "favorite":
            self._load_favorite_leads(force=force)
        else:
            self._load_claimed_leads(force=force)

    def _load_claimed_leads(self, *, force: bool = False):
        if not self._mibuddy_bound:
            self._refresh_list()
            return
        if self._claimed_cache_valid and not force:
            self._refresh_tab_list("claimed")
            self._emit_claimed_leads_fetch(1, self.CLAIMED_FETCH_PAGE_SIZE, False, True)
            return
        self._emit_claimed_leads_fetch(1, self.CLAIMED_FETCH_PAGE_SIZE, False, False)

    def _load_favorite_leads(self, *, force: bool = False):
        if not self._mibuddy_bound:
            self._refresh_list()
            return
        keyword = self._favorite_client_name
        cache_ok = (
            self._favorite_cache_valid
            and self._favorite_cached_client_name == keyword
            and not force
        )
        if cache_ok:
            self._refresh_tab_list("favorite")
            self._emit_favorite_leads_fetch(1, self.favorite_page_size, False, True, keyword)
            return
        silent = bool(self.favorite_leads) and self._favorite_cached_client_name == keyword
        self._emit_favorite_leads_fetch(1, self.favorite_page_size, False, silent, keyword)

    def _list_fingerprint(self, tab: str | None = None) -> tuple:
        tab = tab or self.current_tab
        if tab == "claimed":
            keyword = self.search_box.text().strip().lower()
            src = self.claimed_leads
            total = self.claimed_total
            display_page = self._claimed_display_page
        else:
            keyword = self._favorite_client_name
            src = self.favorite_leads
            total = self.favorite_total
            display_page = 0
        return (
            tab,
            keyword,
            total,
            display_page,
            tuple(
                (
                    row.get("id"),
                    row.get("tags"),
                    row.get("budget"),
                    row.get("followup_time"),
                    row.get("is_favorite"),
                )
                for row in src
            ),
        )

    def _sync_load_more_button(self):
        if self.current_tab == "claimed":
            self.load_more_btn.hide()
            return
        if self._leads_loading or self._favorite_loading_more:
            self.load_more_btn.hide()
            return
        if self._has_more_favorite():
            self.load_more_btn.setText("加载更多")
            self.load_more_btn.setEnabled(True)
            self.load_more_btn.show()
        else:
            self.load_more_btn.hide()

    def set_favorite_leads_loading_more(self, loading: bool):
        self._favorite_loading_more = loading
        if loading:
            self.load_more_btn.setText("加载中...")
            self.load_more_btn.setEnabled(False)
            self.load_more_btn.show()
        else:
            self._sync_load_more_button()

    def _show_leads_skeleton(self):
        if not hasattr(self, "_leads_loading_overlay"):
            return
        self._leads_loading_overlay.setGeometry(self.list_area.rect())
        self._leads_loading_overlay.show()
        self._leads_loading_overlay.raise_()
        self.list_stack.hide()
        self.empty_container.hide()
        self.load_more_btn.hide()
        self.claimed_pagination_bar.hide()
        self._leads_loading_skeleton.start()

    def _hide_leads_skeleton(self):
        if not hasattr(self, "_leads_loading_overlay"):
            return
        self._leads_loading_skeleton.stop()
        self._leads_loading_overlay.hide()

    def set_claimed_leads_loading(self, loading: bool):
        self._leads_loading = loading
        if loading and self.current_tab == "claimed":
            self._show_leads_skeleton()
        elif not loading and self.current_tab == "claimed":
            self._hide_leads_skeleton()

    def set_favorite_leads_loading(self, loading: bool):
        self._leads_loading = loading
        if loading and self.current_tab == "favorite":
            self._show_leads_skeleton()
        elif not loading and self.current_tab == "favorite":
            self._hide_leads_skeleton()

    def set_claimed_leads_page(
        self,
        data: dict | None,
        *,
        append: bool = False,
        preserve_scroll: bool = False,
        seq: int = 0,
        silent: bool = False,
    ):
        if seq and seq != self._claimed_fetch_seq:
            return
        self._leads_loading = False
        self._hide_leads_skeleton()
        data = data or {}
        items = list(data.get("list") or [])
        self.claimed_total = int(data.get("total") or len(items))
        self.claimed_leads = items
        self._claimed_cache_valid = True
        if not silent and not preserve_scroll:
            self._claimed_display_page = 1
        self._refresh_tab_list("claimed", preserve_scroll=preserve_scroll)

    def show_claimed_leads_error(self, message: str):
        self._leads_loading = False
        self._hide_leads_skeleton()
        self.claimed_leads = []
        self.claimed_total = 0
        self._claimed_display_page = 1
        self._claimed_cache_valid = False
        self._rendered_fingerprints.pop("claimed", None)
        if self.current_tab == "claimed":
            self.empty_label.setText(message or "加载认领客资失败")
            self.empty_container.show()
            self.list_stack.hide()
            self.load_more_btn.hide()
            self.claimed_pagination_bar.hide()

    def set_favorite_leads_page(
        self,
        data: dict | None,
        *,
        append: bool = False,
        client_name: str | None = None,
        preserve_scroll: bool = False,
        seq: int = 0,
        silent: bool = False,
    ):
        keyword = (client_name if client_name is not None else self._favorite_client_name) or ""
        if keyword != self._favorite_client_name:
            return
        if seq and seq != self._favorite_fetch_seq:
            return
        self._leads_loading = False
        self._hide_leads_skeleton()
        if not append:
            self._favorite_loading_more = False
        data = data or {}
        items = list(data.get("list") or [])
        page = int(data.get("page") or 1)
        self.favorite_total = int(data.get("total") or 0)
        self.favorite_page = page
        self.favorite_page_size = int(data.get("page_size") or self.favorite_page_size)
        if append:
            self.append_favorite_leads_batch(data, client_name=keyword, seq=seq)
        elif silent and self.favorite_leads and self._favorite_highest_page > 1:
            self.favorite_leads = self._merge_head_page(self.favorite_leads, items)
            self._favorite_cache_valid = True
            self._favorite_cached_client_name = keyword
        else:
            self.favorite_leads = items
            self._favorite_highest_page = page
            self._favorite_cache_valid = True
            self._favorite_cached_client_name = keyword
        self._refresh_tab_list("favorite", preserve_scroll=preserve_scroll or append)

    def show_favorite_leads_error(self, message: str):
        self._leads_loading = False
        self._hide_leads_skeleton()
        self._favorite_loading_more = False
        self.favorite_leads = []
        self.favorite_total = 0
        self._favorite_highest_page = 0
        self._favorite_has_more = False
        self._favorite_cache_valid = False
        self._favorite_cached_client_name = ""
        self._rendered_fingerprints.pop("favorite", None)
        if self.current_tab == "favorite":
            self.empty_label.setText(message or "加载收藏客资失败")
            self.empty_container.show()
            self.list_stack.hide()
            self.load_more_btn.hide()
            self.claimed_pagination_bar.hide()

    def _on_load_more_clicked(self):
        if self._leads_loading or self._favorite_loading_more:
            return
        if self.current_tab != "favorite":
            return
        if not self._has_more_favorite():
            return
        next_page = max(self._favorite_highest_page, 1) + 1
        self._emit_favorite_leads_fetch(
            next_page,
            self.favorite_page_size,
            True,
            False,
            self._favorite_client_name,
        )

    def apply_mibuddy_binding_state(self, data: dict | None):
        data = data or {}
        uuid = str(data.get("uuid") or "").strip()
        was_bound = self._mibuddy_bound
        self._mibuddy_bound = bool(uuid)
        if self._mibuddy_bound:
            if self._awaiting_binding_for_load:
                self.on_binding_synced()
            elif not was_bound:
                self.start_auto_refresh()
            return
        self.stop_auto_refresh()
        self._awaiting_binding_for_load = False
        self.invalidate_leads_cache()
        self.claimed_leads = []
        self.claimed_total = 0
        self.favorite_leads = []
        self.favorite_total = 0
        self._favorite_loading_more = False
        self._refresh_list()

    def _defer_sync_tab_card_widths(self, tab: str | None = None):
        tab = tab or self.current_tab
        lw = self._list_widget_for(tab)

        def _sync():
            if lw.count() > 0:
                self._sync_lead_card_widths(lw)

        QTimer.singleShot(0, _sync)

    def _switch_to_claimed(self):
        self.current_tab = "claimed"
        self._favorite_search_timer.stop()
        self.search_box.setPlaceholderText("搜索单位、地区、电话或姓名...")
        self._show_list_stack_for("claimed")
        self._load_claimed_leads()
        self._sync_visible_tab_chrome()
        self._defer_sync_tab_card_widths("claimed")

    def _switch_to_favorite(self):
        self.current_tab = "favorite"
        self.search_box.setPlaceholderText("搜索单位名称...")
        self._favorite_client_name = self.search_box.text().strip()
        self._show_list_stack_for("favorite")
        self._load_favorite_leads()
        self._sync_visible_tab_chrome()
        self._defer_sync_tab_card_widths("favorite")

    def _filter_list(self):
        if self.current_tab == "claimed":
            self._claimed_display_page = 1
            self._rendered_fingerprints.pop("claimed", None)
            self._refresh_tab_list("claimed")
        else:
            self._favorite_search_timer.start()

    def _on_favorite_search_debounced(self):
        keyword = self.search_box.text().strip()
        if keyword == self._favorite_client_name and self._favorite_cache_valid:
            self._rendered_fingerprints.pop("favorite", None)
            self._refresh_tab_list("favorite")
            return
        self._favorite_client_name = keyword
        self._favorite_cache_valid = False
        self._favorite_cached_client_name = ""
        self._rendered_fingerprints.pop("favorite", None)
        if not self._mibuddy_bound:
            self._refresh_list()
            return
        self._load_favorite_leads(force=True)

    def _unbound_hint_text(self) -> str:
        return "请先在「设置」中绑定米城 UUID 以查看客资"

    def _filtered_leads_for_tab(self, tab: str) -> tuple[list[dict], str, list[dict]]:
        if tab == "favorite":
            keyword = self._favorite_client_name
            leads_source = self.favorite_leads
            return list(leads_source), keyword, leads_source
        keyword = self.search_box.text().strip().lower()
        leads_source = self.claimed_leads
        filtered = []
        for lead in leads_source:
            unit = lead.get("unit_name", "").lower()
            name = lead.get("customer_name", "").lower()
            phone = lead.get("phone", "").lower()
            region = lead.get("region", "").lower()
            if not keyword or (
                keyword in unit or keyword in name or keyword in phone or keyword in region
            ):
                filtered.append(lead)
        return filtered, keyword, leads_source

    def _sync_visible_tab_chrome(self):
        if not self._mibuddy_bound:
            self.list_stack.hide()
            self.empty_label.setText(self._unbound_hint_text())
            self.empty_container.show()
            self.load_more_btn.hide()
            self.claimed_pagination_bar.hide()
            return
        if self._leads_loading:
            return
        filtered, keyword, leads_source = self._filtered_leads_for_tab(self.current_tab)
        if not filtered:
            if keyword and (leads_source or self.current_tab == "favorite"):
                self.empty_label.setText("未找到匹配的客资")
            elif self.current_tab == "claimed":
                self.empty_label.setText("暂无认领客资")
            else:
                self.empty_label.setText("暂无收藏客资")
            self.empty_container.show()
            self.list_stack.hide()
            self.load_more_btn.hide()
            self.claimed_pagination_bar.hide()
            self.claimed_pagination_bar.hide()
            return
        self.empty_container.hide()
        self.list_stack.show()
        self._show_list_stack_for()
        if self.current_tab == "claimed":
            self._sync_claimed_pagination_chrome(len(filtered))
            self.load_more_btn.hide()
        else:
            self.claimed_pagination_bar.hide()
            self._sync_load_more_button()

    def _refresh_list(self, *, preserve_scroll: bool = False):
        self._refresh_tab_list(self.current_tab, preserve_scroll=preserve_scroll)

    def _refresh_tab_list(self, tab: str, *, preserve_scroll: bool = False):
        if not self._mibuddy_bound:
            if tab == self.current_tab:
                for lw in self.iter_leads_list_widgets():
                    lw.clear()
                self._sync_visible_tab_chrome()
            return
        if tab == self.current_tab and self._leads_loading:
            return

        list_widget = self._list_widget_for(tab)
        vbar = list_widget.verticalScrollBar()
        scroll_pos = vbar.value() if preserve_scroll else None
        filtered_leads, keyword, leads_source = self._filtered_leads_for_tab(tab)
        if tab == "claimed":
            leads_to_render = self._slice_claimed_display_page(filtered_leads)
        else:
            leads_to_render = filtered_leads

        fp = self._list_fingerprint(tab)
        if fp == self._rendered_fingerprints.get(tab):
            if tab == self.current_tab:
                self._sync_visible_tab_chrome()
            if list_widget.count() > 0:
                self._sync_lead_card_widths(list_widget)
            return

        list_widget.setUpdatesEnabled(False)
        try:
            self._outbound_call_cards.clear()
            list_widget.clear()
            if leads_to_render:
                is_claimed = tab == "claimed"
                for lead in leads_to_render:
                    item = QListWidgetItem(list_widget)
                    card = LeadCardWidget(lead, is_claimed=is_claimed)
                    card.detail_requested.connect(self._open_detail_dialog)
                    card.changhu_call_requested.connect(self._on_lead_changhu_call_requested)
                    card.yunke_call_requested.connect(self._on_lead_yunke_call_requested)
                    if is_claimed:
                        card.remove_requested.connect(self._on_lead_ignore_requested)
                    lead_id = card.lead_data.get("id")
                    if lead_id is not None:
                        self._outbound_call_cards[int(lead_id)] = card
                    list_widget.addItem(item)
                    list_widget.setItemWidget(item, card)
                self._sync_lead_card_widths(list_widget)
        finally:
            list_widget.setUpdatesEnabled(True)

        self._rendered_fingerprints[tab] = fp
        if scroll_pos is not None:
            vbar.setValue(min(scroll_pos, vbar.maximum()))
        if tab == self.current_tab:
            self._sync_visible_tab_chrome()
            QTimer.singleShot(50, lambda: self.resizeEvent(None))

    def _get_or_create_detail_dialog(self) -> LeadDetailDialog:
        if self._detail_dialog is None:
            dlg = LeadDetailDialog({}, self.window())
            dlg.save_requested.connect(self._on_lead_save_requested)
            dlg.remark_add_requested.connect(self._on_remark_add_requested)
            dlg.tel_approve_requested.connect(self._on_tel_approve_requested)
            dlg.finished.connect(self._on_detail_dialog_finished)
            self._detail_dialog = dlg
        return self._detail_dialog

    def _on_detail_dialog_finished(self, _result: int = 0):
        self._active_detail_dialog = None
        if not self._detail_list_patched:
            self._refresh_list()

    def _open_detail_dialog(self, lead_data: dict):
        self._detail_list_patched = False
        dialog = self._get_or_create_detail_dialog()
        self._active_detail_dialog = dialog
        dialog.load_lead({**lead_data, "followup_records": []})
        dialog.position_beside(self)
        first_show = not dialog.isVisible()
        dialog.show()
        dialog.raise_()
        if first_show:
            dialog.activateWindow()
        lead_id = lead_data.get("id")
        if lead_id is not None:
            dialog.show_remarks_loading()
            self.lead_remarks_fetch_requested.emit(int(lead_id), 1, 50)

    def _on_lead_save_requested(self, payload: dict):
        self.lead_update_requested.emit(payload)

    def _on_remark_add_requested(self, payload: dict):
        self.lead_remark_add_requested.emit(payload)

    def _on_tel_approve_requested(self, lead_id: int):
        self.lead_tel_approve_requested.emit(int(lead_id))

    def _on_lead_changhu_call_requested(self, lead_data: dict, changhu_tel: str):
        lead_id = lead_data.get("id")
        if lead_id is None:
            return
        self._open_detail_dialog(lead_data)
        self.lead_changhu_call_requested.emit(int(lead_id), (changhu_tel or "").strip())

    def _on_lead_yunke_call_requested(self, lead_data: dict):
        lead_id = lead_data.get("id")
        if lead_id is None:
            return
        self._open_detail_dialog(lead_data)
        self.lead_yunke_call_requested.emit(int(lead_id))

    def _on_lead_ignore_requested(self, lead_data: dict):
        lead_id = lead_data.get("id")
        if lead_id is None:
            return
        self.lead_ignore_requested.emit(int(lead_id))

    def handle_changhu_call_result(self, lead_id: int, ok: bool, message: str = ""):
        card = self._outbound_call_cards.get(int(lead_id))
        if card is not None:
            card.handle_changhu_call_result(ok, message)

    def handle_yunke_call_result(self, lead_id: int, ok: bool, message: str = ""):
        card = self._outbound_call_cards.get(int(lead_id))
        if card is not None:
            card.handle_yunke_call_result(ok, message)

    def handle_lead_ignore_result(self, lead_id: int, ok: bool, message: str = ""):
        card = self._outbound_call_cards.get(int(lead_id))
        if card is not None:
            card.handle_ignore_result(ok, message)
        if not ok:
            return
        lead_data = next((x for x in self.claimed_leads if x.get("id") == lead_id), None)
        unit_name = (lead_data or {}).get("unit_name") or "该客资"
        self.claimed_leads = [x for x in self.claimed_leads if x.get("id") != lead_id]
        self.claimed_total = max(0, self.claimed_total - 1)
        self._outbound_call_cards.pop(int(lead_id), None)
        self._rendered_fingerprints.pop("claimed", None)
        self._refresh_tab_list("claimed")
        InfoBar.success(
            title="已移除",
            content=f"已成功将客资「{unit_name}」移出认领列表",
            duration=2500,
            position=InfoBarPosition.TOP,
            parent=self.window(),
        )

    def _normalize_lead_display(self, info: dict) -> dict:
        out = dict(info or {})
        budget = out.get("budget")
        if budget not in (None, "", "待设置"):
            try:
                num = int(float(str(budget).replace(",", "").replace("，", "").replace("元", "").strip()))
                out["budget"] = f"{num:,} 元"
            except (TypeError, ValueError):
                pass
        month = str(out.get("purchase_month") or "").strip()
        if month and month != "待设置" and "月" not in month:
            try:
                m = int(month)
                if 1 <= m <= 12:
                    out["purchase_month"] = f"{m}月"
            except (TypeError, ValueError):
                pass
        followup = str(out.get("followup_time") or "").strip()
        if followup in ("", "待设置"):
            out["followup_time"] = "待设置"
        wechat = str(out.get("wechat_id") or "").strip()
        if not wechat:
            out["wechat_id"] = "待设置"
        return out

    def _find_lead_by_id(self, lead_id):
        for lst in (self.claimed_leads, self.favorite_leads):
            for row in lst:
                if row.get("id") == lead_id:
                    return row
        return None

    def _patch_lead_in_list(self, lead_id):
        row = self._find_lead_by_id(lead_id)
        if not row:
            return
        for lw in self.iter_leads_list_widgets():
            for i in range(lw.count()):
                item = lw.item(i)
                card = lw.itemWidget(item)
                if card and card.lead_data.get("id") == lead_id:
                    card.refresh_from_data(row)
                    target_w = self._lead_card_target_width(lw)
                    if target_w > 0:
                        self._sync_lead_card_item_geometry(item, card, target_w)
                        lw.doItemsLayout()
                        lw.viewport().update()
                    break

    def _sync_favorite_list_optimistic(self, lead_id, info: dict):
        """收藏状态变更时先乐观更新收藏列表，避免切页仍看到旧数据。"""
        if "is_favorite" not in info:
            return
        is_fav = bool(info.get("is_favorite"))
        base = self._find_lead_by_id(lead_id) or {}
        merged = {**base, **self._normalize_lead_display(info), "is_favorite": is_fav}
        if is_fav:
            replaced = False
            for i, row in enumerate(self.favorite_leads):
                if row.get("id") == lead_id:
                    self.favorite_leads[i] = {**row, **merged}
                    replaced = True
                    break
            if not replaced:
                self.favorite_leads.insert(0, merged)
                self.favorite_total = max(self.favorite_total + 1, len(self.favorite_leads))
        else:
            old_len = len(self.favorite_leads)
            self.favorite_leads = [x for x in self.favorite_leads if x.get("id") != lead_id]
            if len(self.favorite_leads) < old_len:
                self.favorite_total = max(0, self.favorite_total - 1)
        self._favorite_cache_valid = True
        self._rendered_fingerprints.pop("favorite", None)
        self._refresh_tab_list("favorite")

    def handle_lead_update_result(self, ok: bool, message: str = "", payload: dict | None = None):
        dialog = self._active_detail_dialog
        if dialog is not None:
            dialog.handle_save_result(ok, message)
        if ok and payload:
            lead_id = payload.get("lead_id")
            info = self._normalize_lead_display(payload.get("info") or {})
            self._merge_lead_form(lead_id, info)
            self._rendered_fingerprints.pop("claimed", None)
            self._rendered_fingerprints.pop("favorite", None)
            self._sync_favorite_list_optimistic(lead_id, info)
            self._patch_lead_in_list(lead_id)
            if self.current_tab == "claimed":
                self._rendered_fingerprints["claimed"] = self._list_fingerprint("claimed")
            self._detail_list_patched = True
            self._request_background_sync()

    def _merge_lead_form(self, lead_id, info: dict):
        if lead_id is None:
            return
        for lst in (self.claimed_leads, self.favorite_leads):
            for i, row in enumerate(lst):
                if row.get("id") == lead_id:
                    lst[i] = {**row, **info}
                    break

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_leads_loading_overlay") and self._leads_loading_overlay.isVisible():
            self._leads_loading_overlay.setGeometry(self.list_area.rect())
        for lw in self.iter_leads_list_widgets():
            self._sync_lead_card_widths(lw)
        dlg = self._active_detail_dialog
        if dlg is not None and dlg.isVisible():
            dlg.position_beside(self)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(50, lambda: self.resizeEvent(None))

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        bg_color = "#202020" if is_dark else "#f9f9f9"
        text_main = "#e8e8e8" if is_dark else "#333333"
        text_sub = "#999999" if is_dark else "#888888"
        self.setStyleSheet(f"QFrame#CustomerLeadsPage {{ background-color: {bg_color}; }}")
        style_label(self.title_lbl, "page_title", color=text_main)
        style_label(self.empty_label, "empty", color=text_sub)
        style_label(self.claimed_page_info, "empty", color=text_sub)
