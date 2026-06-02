"""编辑后发送到微信：多行文本编辑 + 顶部摘要 + emoji 表情选择。"""

import os
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QWidget, QGridLayout, QToolButton, QScrollArea
from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QIcon
from qfluentwidgets import (
    BodyLabel, CaptionLabel, TextEdit, 
    PrimaryPushButton, PushButton, TransparentPushButton, 
    isDarkTheme
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
            
            btn.setToolTip(emoji_code) # 悬停时展示微信表情代号（例如：[微笑]）
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


class WechatSendEditDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        original_text: str,
        summary_lines: list[str],
    ):
        super().__init__(parent)
        self.setWindowTitle("编辑后发送")
        self.resize(400, 320)

        layout = QVBoxLayout(self)
        for i, line in enumerate(summary_lines or []):
            lab = BodyLabel(line) if i == 0 else CaptionLabel(line)
            lab.setWordWrap(True)
            layout.addWidget(lab)

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
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        layout.addLayout(row)

        btn_cancel.clicked.connect(self.reject)
        btn_ok.clicked.connect(self.accept)

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
