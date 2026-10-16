"""融合笔记展示（Markdown 渲染）。"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QTextEdit, QFrame, QHBoxLayout,
)

from pc_api.gui.styles import Metrics, get_palette
from pc_api.gui.icons import icon_pixmap


class NoteView(QFrame):
    """融合笔记展示区，用 QTextEdit 的 Markdown 渲染。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setFrameShape(QFrame.Shape.NoFrame)

        layout = QVBoxLayout(self)
        layout.setSpacing(Metrics.SPACING_MD)
        layout.setContentsMargins(
            Metrics.SPACING_MD, Metrics.SPACING_MD,
            Metrics.SPACING_MD, Metrics.SPACING_MD
        )

        title_row = QHBoxLayout()
        title_row.setSpacing(Metrics.SPACING_SM)
        icon_lbl = QLabel()
        icon_lbl.setPixmap(icon_pixmap("note", 18, get_palette().PRIMARY))
        title_row.addWidget(icon_lbl)
        title = QLabel("融合笔记")
        title.setObjectName("titleLabel")
        title_row.addWidget(title)
        title_row.addStretch()
        layout.addLayout(title_row)

        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setPlaceholderText("转写与图片描述完成后，点击「生成融合笔记」…")
        layout.addWidget(self.text_edit)

    def set_note(self, markdown_text: str) -> None:
        """展示 Markdown 笔记。QTextEdit.setMarkdown 支持基本 Markdown。"""
        if markdown_text:
            self.text_edit.setMarkdown(markdown_text)
        else:
            self.text_edit.clear()

    def set_loading(self, msg: str = "生成中…") -> None:
        self.text_edit.setPlainText(msg)

    def clear(self) -> None:
        self.text_edit.clear()
