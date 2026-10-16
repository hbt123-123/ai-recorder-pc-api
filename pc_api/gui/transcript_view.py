"""转写文本展示：按说话人分段渲染为聊天气泡。

有 chunks（带时间戳/说话人）时按段渲染气泡，无 chunks 时回退纯文本。
气泡可点击（chunk_clicked 信号携带 start_time），配合 AudioPlayer 实现
「点转写文字跳到对应时间点」；播放进度可用 set_highlight 高亮当前段落。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QScrollArea,
)

from pc_api.gui.styles import Metrics, get_palette
from pc_api.gui.icons import icon_pixmap


def _fmt_time(start: float, end) -> str:
    """格式化时间段：m:ss 或 m:ss–m:ss。"""
    s = int(max(0, start))
    m, ss = divmod(s, 60)
    if end is None:
        return f"{m}:{ss:02d}"
    e = int(max(0, end))
    m2, ss2 = divmod(e, 60)
    return f"{m}:{ss:02d}–{m2}:{ss2:02d}"


class _Bubble(QFrame):
    """单条转写气泡：说话人 + 时间段 + 内容。"""

    clicked = Signal(float)  # start_time

    def __init__(self, text: str, start, end, speaker: str,
                 align_right: bool, parent=None):
        super().__init__(parent)
        self._start = start
        self.setObjectName("BubbleSelf" if align_right else "BubbleOther")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("highlighted", "false")
        self.setMaximumWidth(760)

        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(
            Metrics.SPACING_SM + 4, Metrics.SPACING_XS,
            Metrics.SPACING_SM + 4, Metrics.SPACING_XS,
        )

        top = QHBoxLayout()
        top.setSpacing(Metrics.SPACING_SM)
        speaker_lbl = QLabel(speaker or "转写")
        speaker_lbl.setObjectName("bubbleSpeaker")
        top.addWidget(speaker_lbl)
        top.addStretch()
        if start is not None:
            time_lbl = QLabel(_fmt_time(start, end))
            time_lbl.setObjectName("bubbleTime")
            top.addWidget(time_lbl)
        layout.addLayout(top)

        content = QLabel(text)
        content.setObjectName("bubbleText")
        content.setWordWrap(True)
        layout.addWidget(content)

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self._start is not None):
            self.clicked.emit(self._start)
        super().mouseReleaseEvent(event)

    def set_highlight(self, on: bool) -> None:
        self.setProperty("highlighted", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class TranscriptView(QFrame):
    """转写文本展示区：chunk 气泡渲染 + 点击跳转 + 播放高亮。"""

    chunk_clicked = Signal(float)

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

        # 标题行（text 图标 + 文本）
        title_row = QHBoxLayout()
        title_row.setSpacing(Metrics.SPACING_SM)
        icon_lbl = QLabel()
        icon_lbl.setPixmap(icon_pixmap("text", 18, get_palette().PRIMARY))
        title_row.addWidget(icon_lbl)
        title = QLabel("转写文本")
        title.setObjectName("titleLabel")
        title_row.addWidget(title)
        title_row.addStretch()
        layout.addLayout(title_row)

        # 气泡列表滚动区
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.container = QWidget()
        self.vlayout = QVBoxLayout(self.container)
        self.vlayout.setSpacing(Metrics.SPACING_XS)
        self.vlayout.setContentsMargins(4, 4, 4, 4)
        self.vlayout.addStretch()  # 底部吸底，气泡从顶部排列
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        # 纯文本 / loading 回退标签
        self._fallback_lbl = QLabel("转写结果将显示在此处…")
        self._fallback_lbl.setObjectName("subtitleLabel")
        self._fallback_lbl.setWordWrap(True)
        self._fallback_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._fallback_lbl.setVisible(False)
        layout.addWidget(self._fallback_lbl)

        # 增量渲染状态
        self._bubbles: list = []        # (start, end, _Bubble)
        self._last_speaker: str = None
        self._align_right = False       # 当前气泡方向（说话人变化时翻转）
        self._partial_lbl: QLabel = None
        self._highlighted_idx = None    # 当前高亮气泡索引（None=无，避免重复 repolish）

    # ---- 布局 ----

    def _clear_bubbles(self) -> None:
        """移除全部气泡 + partial，保留 stretch。"""
        for _start, _end, b in self._bubbles:
            self.vlayout.removeWidget(b)
            b.deleteLater()
        self._bubbles.clear()
        self._last_speaker = None
        self._align_right = False
        self._highlighted_idx = None
        if self._partial_lbl is not None:
            self.vlayout.removeWidget(self._partial_lbl)
            self._partial_lbl.deleteLater()
            self._partial_lbl = None

    def _append_bubble(self, text: str, start, end, speaker) -> None:
        """追加一条气泡；说话人变化时翻转左右方向。"""
        if not text or not text.strip():
            return
        if speaker != self._last_speaker:
            self._align_right = not self._align_right
            self._last_speaker = speaker
        bubble = _Bubble(text.strip(), start, end, speaker,
                         self._align_right, self.container)
        bubble.clicked.connect(self.chunk_clicked.emit)
        self._bubbles.append((start, end, bubble))
        # 插入到 stretch（count-1）之前
        self.vlayout.insertWidget(self.vlayout.count() - 1, bubble)
        self._scroll_to_bottom()

    def _show_partial(self, text: str) -> None:
        """底部实时生长句（灰斜体）。"""
        if not text:
            if self._partial_lbl is not None:
                self._partial_lbl.setText("")
            return
        if self._partial_lbl is None:
            self._partial_lbl = QLabel()
            self._partial_lbl.setObjectName("bubblePartial")
            self._partial_lbl.setWordWrap(True)
            self.vlayout.insertWidget(self.vlayout.count() - 1, self._partial_lbl)
        self._partial_lbl.setText(text)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ---- 公开 API ----

    def set_transcription(self, text: str, chunks=None) -> None:
        """展示转写结果。有 chunks 则按说话人渲染气泡，否则纯文本。"""
        self._clear_bubbles()
        if chunks:
            self._fallback_lbl.setVisible(False)
            for c in chunks:
                self._append_bubble(
                    c.content, c.start_time, c.end_time,
                    c.speaker_name or "转写",
                )
        else:
            self._fallback_lbl.setVisible(True)
            self._fallback_lbl.setText(text if text else "暂无转写内容")
            self._fallback_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
            self._fallback_lbl.setWordWrap(True)
            if not text:
                self._fallback_lbl.setText("转写结果将显示在此处…")
                self._fallback_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_loading(self, msg: str = "转写中…") -> None:
        """显示 loading / 提示信息（无气泡场景）。"""
        self._clear_bubbles()
        self._fallback_lbl.setVisible(True)
        self._fallback_lbl.setText(msg)
        self._fallback_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def append_final(self, text: str, start_time=None, end_time=None) -> None:
        """增量追加一条最终句（说话人未知 → None）。"""
        if not text:
            return
        self._fallback_lbl.setVisible(False)
        self._append_bubble(text, start_time, end_time, None)

    def set_partial(self, text: str) -> None:
        """设置当前生长句的中间结果（实时刷新）。"""
        self._show_partial(text or "")

    def set_highlight(self, time_seconds) -> None:
        """高亮包含 time_seconds 的气泡并滚动到可见（播放进度驱动）。"""
        if not self._bubbles:
            return
        target_idx = None
        for idx, (start, end, _b) in enumerate(self._bubbles):
            if (start is not None and start <= time_seconds
                    and (end is None or time_seconds <= end)):
                target_idx = idx  # 不提前退出，保持与旧逻辑一致取最后命中段
        if target_idx == self._highlighted_idx:
            return  # 高亮未变化，跳过 repolish
        # 只切换旧/新两个气泡的高亮属性，避免全量 repolish（索引越界时静默跳过）
        if (self._highlighted_idx is not None
                and 0 <= self._highlighted_idx < len(self._bubbles)):
            self._bubbles[self._highlighted_idx][2].set_highlight(False)
        self._highlighted_idx = target_idx
        if target_idx is not None:
            self._bubbles[target_idx][2].set_highlight(True)
            self.scroll.ensureWidgetVisible(self._bubbles[target_idx][2], 40, 40)

    def clear(self) -> None:
        self._clear_bubbles()
        self._fallback_lbl.setVisible(True)
        self._fallback_lbl.setText("转写结果将显示在此处…")
        self._fallback_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
