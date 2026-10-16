"""附件（照片 + 截屏 + VLM 描述）展示。"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QScrollArea, QFrame, QHBoxLayout,
)

from pc_api.gui.styles import Metrics, get_palette
from pc_api.gui.icons import icon_pixmap


# 来源 → 标题前缀映射
_SOURCE_LABEL = {
    "camera": "拍照",
    "screenshot": "截屏",
    "file": "图片",
}


def _source_title(source: str, timestamp: float) -> str:
    """生成附件卡片标题，区分来源。"""
    prefix = _SOURCE_LABEL.get(source, "图片")
    return f"{prefix} @ {timestamp:.1f}s"


class AttachmentCard(QFrame):
    """单张照片/截屏 + 描述卡片。"""

    def __init__(self, image_path: str, timestamp: float, description: str = "",
                 source: str = "camera", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setFrameShape(QFrame.Shape.NoFrame)

        layout = QHBoxLayout(self)
        layout.setSpacing(Metrics.SPACING_MD)
        layout.setContentsMargins(
            Metrics.SPACING_MD, Metrics.SPACING_MD,
            Metrics.SPACING_MD, Metrics.SPACING_MD
        )

        lbl_img = QLabel()
        pix = QPixmap(image_path)
        if not pix.isNull():
            lbl_img.setPixmap(pix.scaled(
                140, 140, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            ))
        lbl_img.setMinimumSize(120, 120)
        lbl_img.setMaximumSize(160, 160)
        lbl_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_img)

        right = QVBoxLayout()
        right.setSpacing(Metrics.SPACING_XS)

        lbl_title = QLabel(_source_title(source, timestamp))
        lbl_title.setObjectName("cardTitle")
        right.addWidget(lbl_title)

        self.desc_label = QLabel(description or "（描述生成中…）")
        self.desc_label.setObjectName("subtitleLabel")
        self.desc_label.setWordWrap(True)
        self.desc_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        right.addWidget(self.desc_label)
        right.addStretch()
        layout.addLayout(right, 1)

    def set_description(self, desc: str) -> None:
        self.desc_label.setText(desc or "（无描述）")


class AttachmentPanel(QFrame):
    """附件列表面板：纵向排列 AttachmentCard。"""

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
        icon_lbl.setPixmap(icon_pixmap("image", 18, get_palette().PRIMARY))
        title_row.addWidget(icon_lbl)
        title = QLabel("照片附件")
        title.setObjectName("titleLabel")
        title_row.addWidget(title)
        title_row.addStretch()
        layout.addLayout(title_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.container = QWidget()
        self.vlayout = QVBoxLayout(self.container)
        self.vlayout.setSpacing(Metrics.SPACING_SM)
        self.vlayout.setContentsMargins(0, 0, 0, 0)
        self.vlayout.addStretch()
        # 空状态引导提示（上下各一 stretch 使其垂直居中）
        self._empty_hint = QLabel("录音中点击拍照 / 截屏，或用「添加图片」上传")
        self._empty_hint.setObjectName("subtitleLabel")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.vlayout.addWidget(self._empty_hint)
        self.vlayout.addStretch()
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll)

        self._cards = {}  # attachment_id -> AttachmentCard

    @property
    def count(self) -> int:
        """当前附件卡片数量。"""
        return len(self._cards)

    def clear(self) -> None:
        # 仅移除卡片，保留 hint + stretch 结构
        for card in self._cards.values():
            self.vlayout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        self._empty_hint.show()

    def add_attachment(self, attachment_id: int, image_path: str,
                       timestamp: float, description: str = "",
                       source: str = "camera") -> None:
        card = AttachmentCard(image_path, timestamp, description, source=source)
        self._empty_hint.hide()
        self.vlayout.insertWidget(self.vlayout.count() - 1, card)
        self._cards[attachment_id] = card

    def update_description(self, attachment_id: int, description: str) -> None:
        card = self._cards.get(attachment_id)
        if card:
            card.set_description(description)

    def has_attachments(self) -> bool:
        return len(self._cards) > 0
