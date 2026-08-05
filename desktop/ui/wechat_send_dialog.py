"""编辑后发送到微信：多行文本编辑 + 顶部摘要 + emoji 表情选择 + 历史发送改稿。"""

import os
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
    QGridLayout,
    QToolButton,
    QScrollArea,
    QListWidgetItem,
)
from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QIcon, QTextCursor
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    TextEdit,
    PrimaryPushButton,
    PushButton,
    TransparentPushButton,
    ListWidget,
    isDarkTheme,
)


class EmojiPickerPopup(QWidget):
    emoji_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_StyledBackground, True)

        # 主垂直布局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(2, 2, 2, 2)

        # 滚动区域
        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")

        # 网格内容容器
        content_widget = QWidget()
        content_widget.setObjectName("EmojiContentWidget")
        grid_layout = QGridLayout(content_widget)
        grid_layout.setContentsMargins(6, 6, 6, 6)
        grid_layout.setSpacing(4)

        # 微信常用表情代码列表
        emojis = [
            "[微笑]","[撇嘴]","[色]","[发呆]","[得意]","[流泪]","[害羞]","[闭嘴]","[睡]","[大哭]","[尴尬]","[发怒]","[调皮]","[呲牙]","[惊讶]","[难过]","[囧]","[抓狂]","[吐]","[偷笑]","[愉快]","[白眼]","[傲慢]","[困]","[惊恐]","[憨笑]","[悠闲]","[咒骂]","[疑问]","[嘘]","[晕]","[衰]","[骷髅]","[敲打]","[再见]","[擦汗]","[抠鼻]","[鼓掌]","[坏笑]","[右哼哼]","[鄙视]","[委屈]","[快哭了]","[阴险]","[亲亲]","[可怜]","[笑脸]","[生病]","[脸红]","[破涕为笑]","[恐惧]","[失望]","[无语]","[嘿哈]","[捂脸]","[奸笑]","[机智]","[皱眉]","[耶]","[吃瓜]","[加油]","[汗]","[天啊]","[Emm]","[社会社会]","[旺柴]","[好的]","[打脸]","[哇]","[翻白眼]","[666]","[让我看看]","[叹气]","[苦涩]","[裂开]","[嘴唇]","[爱心]","[心碎]","[拥抱]","[强]","[弱]","[握手]","[胜利]","[抱拳]","[勾引]","[拳头]","[OK]","[合十]","[啤酒]","[咖啡]","[蛋糕]","[玫瑰]","[凋谢]","[菜刀]","[炸弹]","[便便]","[月亮]","[太阳]","[庆祝]","[礼物]","[红包]","[發]","[福]","[烟花]","[爆竹]","[猪头]","[跳跳]","[发抖]","[转圈]"
        ]

        # 本地表情图片资源目录
        emoji_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "emojis")

        columns = 8
        for index, emoji_code in enumerate(emojis):
            row = index // columns
            col = index % columns
            btn = QToolButton(content_widget)

            # 从本地读取对应的微信 PNG 图片资源
            emoji_name = emoji_code[1:-1]
            file_name = f"{index+1:03d}_{emoji_name}.png"
            image_path = os.path.join(emoji_dir, file_name)

            if os.path.exists(image_path):
                btn.setIcon(QIcon(image_path))
                btn.setIconSize(QSize(24, 24))
            else:
                # 备用显示文字
                btn.setText(emoji_name)

            btn.setToolTip(emoji_code)  # 悬停时展示微信表情代号（例如：[微笑]）
            btn.setFixedSize(30, 30)
            btn.setCursor(Qt.PointingHandCursor)

            # 点击时插入原本的微信表情代码文本
            btn.clicked.connect(lambda checked=False, e=emoji_code: self.on_emoji_clicked(e))
            grid_layout.addWidget(btn, row, col)

        scroll_area.setWidget(content_widget)
        main_layout.addWidget(scroll_area)

        # 设置适中的大小，带滚动条展示
        self.setFixedSize(290, 240)
        self._apply_theme_style()

    def on_emoji_clicked(self, emoji: str):
        self.emoji_selected.emit(emoji)
        self.close()

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        bg_color = "#2c2c2c" if is_dark else "#ffffff"
        border_color = "#3a3a3a" if is_dark else "#e5e5e5"
        text_color = "#ffffff" if is_dark else "#000000"
        hover_bg = "rgba(255, 255, 255, 0.08)" if is_dark else "rgba(0, 0, 0, 0.05)"

        self.setStyleSheet(f"""
            EmojiPickerPopup {{
                background-color: {bg_color};
                border: 1px solid {border_color};
                border-radius: 8px;
            }}
            #EmojiContentWidget {{
                background-color: transparent;
            }}
            QToolButton {{
                background-color: transparent;
                border: none;
                border-radius: 4px;
                color: {text_color};
            }}
            QToolButton:hover {{
                background-color: {hover_bg};
            }}
        """)


_STATUS_LABEL = {
    "sent": "成功",
    "failed": "失败",
    "blocked": "拦截",
    "pending": "待回写",
}


def _history_preview(text: str, max_len: int = 36) -> str:
    one_line = " ".join((text or "").split())
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 1] + "…"


def _history_time(item: dict) -> str:
    raw = (item.get("completed_at") or item.get("created_at") or "").strip()
    if not raw:
        return ""
    # 展示到分钟即可
    if len(raw) >= 16:
        return raw[:16]
    return raw


class WechatSendEditDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        original_text: str,
        summary_lines: list[str],
        history_items: list[dict] | None = None,
        history_scope: str = "",
    ):
        super().__init__(parent)
        self.setWindowTitle("编辑后发送")
        self.resize(460, 460)

        layout = QVBoxLayout(self)
        self._summary_labels: list = []
        for i, line in enumerate(summary_lines or []):
            lab = BodyLabel(line) if i == 0 else CaptionLabel(line)
            lab.setWordWrap(True)
            layout.addWidget(lab)
            self._summary_labels.append(lab)

        self._history_items = list(history_items or [])
        self._history_hint = CaptionLabel(self)
        self._history_hint.setWordWrap(True)
        self._history_list = ListWidget(self)
        self._history_list.setMaximumHeight(110)
        self._history_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._history_list.itemClicked.connect(self._on_history_item_clicked)
        self._history_list.itemDoubleClicked.connect(self._on_history_item_clicked)
        layout.addWidget(self._history_hint)
        layout.addWidget(self._history_list)
        self._populate_history(self._history_items, history_scope)

        self._edit = TextEdit(self)
        self._edit.setPlainText(original_text or "")
        self._edit.setMinimumHeight(160)
        layout.addWidget(self._edit, 1)

        row = QHBoxLayout()

        # 添加 emoji 表情按钮
        self.btn_emoji = TransparentPushButton("😊", self)
        self.btn_emoji.setToolTip("添加表情")
        self.btn_emoji.setStyleSheet("font-size: 16px; padding: 4px;")
        self.btn_emoji.clicked.connect(self._show_emoji_picker)
        row.addWidget(self.btn_emoji)

        row.addStretch()
        btn_cancel = PushButton("取消")
        btn_ok = PrimaryPushButton("确认发送")
        self._btn_ok = btn_ok
        self._btn_cancel = btn_cancel
        self._accepting = False
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        layout.addLayout(row)

        btn_cancel.clicked.connect(self.reject)
        btn_ok.clicked.connect(self._on_confirm_send)
        self._apply_theme_style()

    def _apply_theme_style(self):
        is_dark = isDarkTheme()
        bg = "#1a1a1a" if is_dark else "#f0f2f5"
        text = "#ffffff" if is_dark else "#1a1a1a"
        sub = "#aaaaaa" if is_dark else "#888888"
        self.setStyleSheet(f"QDialog {{ background-color: {bg}; color: {text}; }}")
        for i, lab in enumerate(self._summary_labels):
            lab.setStyleSheet(f"color: {text if i == 0 else sub};")
        self._history_hint.setStyleSheet(f"color: {sub};")

    def _populate_history(self, items: list[dict], scope: str = ""):
        self._history_list.clear()
        usable = []
        seen: set[str] = set()
        for raw in items or []:
            text = str((raw or {}).get("edited_text") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            usable.append(dict(raw or {}, edited_text=text))

        if not usable:
            self._history_hint.setText("暂无历史发送记录（成功/失败均可复用改稿）")
            self._history_list.hide()
            return

        if scope == "customer":
            hint = "历史发送（当前客户；点击填入编辑框，可再修改）"
        else:
            hint = "历史发送（点击填入编辑框，可再修改）"
        self._history_hint.setText(hint)
        self._history_list.show()

        for item in usable:
            status = _STATUS_LABEL.get(str(item.get("status") or ""), str(item.get("status") or ""))
            when = _history_time(item)
            preview = _history_preview(item.get("edited_text") or "")
            label = " · ".join(p for p in (status, when, preview) if p)
            row = QListWidgetItem(label)
            tip_parts = [
                f"状态：{status or '-'}",
                f"时间：{item.get('completed_at') or item.get('created_at') or '-'}",
            ]
            err = (item.get("error") or "").strip()
            if err:
                tip_parts.append(f"错误：{err}")
            tip_parts.append("")
            tip_parts.append(item.get("edited_text") or "")
            row.setToolTip("\n".join(tip_parts))
            row.setData(Qt.ItemDataRole.UserRole, item.get("edited_text") or "")
            self._history_list.addItem(row)

    def _on_confirm_send(self):
        """防双击：确认发送只接受一次。"""
        if self._accepting:
            return
        self._accepting = True
        self._btn_ok.setEnabled(False)
        self._btn_cancel.setEnabled(False)
        self.accept()

    def _on_history_item_clicked(self, item: QListWidgetItem):
        text = item.data(Qt.ItemDataRole.UserRole) if item is not None else ""
        text = (text or "").strip()
        if not text:
            return
        self._edit.setPlainText(text)
        self._edit.setFocus()
        cursor = self._edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._edit.setTextCursor(cursor)

    def _show_emoji_picker(self):
        self.emoji_picker = EmojiPickerPopup(self)
        self.emoji_picker.emoji_selected.connect(self._insert_emoji)

        popup_size = self.emoji_picker.size()
        gap = 4
        button_bottom_left = self.btn_emoji.mapToGlobal(self.btn_emoji.rect().bottomLeft())
        button_top_left = self.btn_emoji.mapToGlobal(self.btn_emoji.rect().topLeft())

        x = button_bottom_left.x()
        y = button_bottom_left.y() + gap

        screen = self.btn_emoji.screen().availableGeometry()
        if x + popup_size.width() > screen.right():
            x = max(screen.left(), screen.right() - popup_size.width())
        if x < screen.left():
            x = screen.left()
        if y + popup_size.height() > screen.bottom():
            y = button_top_left.y() - popup_size.height() - gap

        self.emoji_picker.move(x, y)
        self.emoji_picker.show()

    def _insert_emoji(self, emoji: str):
        cursor = self._edit.textCursor()
        cursor.insertText(emoji)
        self._edit.setTextCursor(cursor)
        self._edit.setFocus()

    def edited_text(self) -> str:
        return (self._edit.toPlainText() or "").strip()
