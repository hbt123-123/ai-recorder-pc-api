"""录音悬浮小窗（视频会议模式）。

无边框、始终置顶的悬浮控件，会议录音时主窗口收起为这个小窗，
仅保留核心控件：开始/停止、计时、展开。

设计要点：
- 悬浮窗只发信号，不直接调 RecordingService（避免双写状态）。
  MainWindow 收到信号后转发到当前 tier 页的 RecordingWidget。
- 圆角背景通过 paintEvent 自绘 + WA_TranslucentBackground 实现。
- 拖拽通过重写 mousePressEvent / mouseMoveEvent 记录偏移。
- 录音状态、计时由 MainWindow 通过方法注入同步。
- 精简尺寸 ~200×56，只留核心按钮和时长。
"""

from PySide6.QtCore import Qt, Signal, QPoint
from PySide6.QtGui import QPainter, QColor, QBrush, QPen, QIcon
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QPushButton, QLabel,
)

from pc_api.gui.styles import get_palette
from pc_api.gui.icons import make_icon


class FloatingWindow(QWidget):
    """录音悬浮小窗：无边框置顶，可拖拽。

    信号：
        recording_started: 用户点录音按钮，请求开始录音
        recording_stopped: 用户点停止按钮，请求停止录音
        expand_requested: 用户点展开，请求展开主窗口
    """

    recording_started = Signal()
    recording_stopped = Signal()
    pause_toggled = Signal()
    expand_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(230, 56)

        # 窗口图标
        from pathlib import Path as _P
        _icon = _P(__file__).resolve().parent.parent.parent / "assets" / "app.ico"
        if _icon.exists():
            self.setWindowIcon(QIcon(str(_icon)))

        self._is_recording = False
        self._is_paused = False
        self._drag_offset = QPoint()

        self._build_ui()
        self._set_recording_state(False)

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        p = get_palette()

        # 开始/停止按钮（同一按钮，按状态切换图标）
        self.btn_toggle = QPushButton()
        self.btn_toggle.setFixedSize(32, 32)
        self.btn_toggle.setObjectName("primaryButton")
        self.btn_toggle.setIcon(make_icon("record", 18, p.TEXT_ON_PRIMARY))
        self.btn_toggle.clicked.connect(self._on_toggle_clicked)
        layout.addWidget(self.btn_toggle)

        # 暂停按钮
        self.btn_pause = QPushButton()
        self.btn_pause.setFixedSize(28, 28)
        self.btn_pause.setObjectName("toolButton")
        self.btn_pause.setIcon(make_icon("pause", 16))
        self.btn_pause.setToolTip("暂停 / 继续")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self.pause_toggled.emit)
        layout.addWidget(self.btn_pause)

        # 中间：计时
        self.lbl_timer = QLabel("0.0s")
        self.lbl_timer.setStyleSheet(
            f"color: {p.TEXT_PRIMARY}; font-size: 13px; font-weight: 600;"
        )
        self.lbl_timer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_timer, 1)

        # 展开按钮
        self.btn_expand = QPushButton()
        self.btn_expand.setFixedSize(28, 28)
        self.btn_expand.setToolTip("展开主窗口")
        self.btn_expand.setObjectName("toolButton")
        self.btn_expand.setIcon(make_icon("expand", 16))
        self.btn_expand.clicked.connect(self.expand_requested.emit)
        layout.addWidget(self.btn_expand)

    # ---- 状态同步（由 MainWindow 调用） ----

    def set_recording_state(self, is_recording: bool) -> None:
        """同步录音状态：切换按钮图标 + 启用/禁用。"""
        self._is_recording = is_recording
        self._set_recording_state(is_recording)
        self.btn_pause.setEnabled(is_recording)
        if not is_recording:
            self._is_paused = False
            self.btn_pause.setIcon(make_icon("pause", 16))

    def set_paused_state(self, is_paused: bool) -> None:
        """同步暂停状态：切换暂停按钮图标和计时颜色。"""
        self._is_paused = is_paused
        p = get_palette()
        if is_paused:
            self.btn_pause.setIcon(make_icon("play", 16))
            self.lbl_timer.setStyleSheet(
                f"color: {p.TEXT_MUTED}; font-size: 13px; font-weight: 600;"
            )
        else:
            self.btn_pause.setIcon(make_icon("pause", 16))
            self.lbl_timer.setStyleSheet(
                f"color: {p.ERROR}; font-size: 13px; font-weight: 600;"
            )

    def _set_recording_state(self, is_recording: bool) -> None:
        p = get_palette()
        if is_recording:
            self.btn_toggle.setIcon(make_icon("stop", 18, p.TEXT_ON_PRIMARY))
            self.btn_toggle.setObjectName("dangerButton")
            self.lbl_timer.setStyleSheet(
                f"color: {p.ERROR}; font-size: 13px; font-weight: 600;"
            )
        else:
            self.btn_toggle.setIcon(make_icon("record", 18, p.TEXT_ON_PRIMARY))
            self.btn_toggle.setObjectName("primaryButton")
            self.lbl_timer.setStyleSheet(
                f"color: {p.TEXT_PRIMARY}; font-size: 13px; font-weight: 600;"
            )
        # 刷新 QSS（objectName 变化需重新 polish）
        self.btn_toggle.style().unpolish(self.btn_toggle)
        self.btn_toggle.style().polish(self.btn_toggle)

    def update_elapsed(self, seconds: float) -> None:
        """更新计时显示。"""
        self.lbl_timer.setText(f"{seconds:.1f}s")

    # ---- 按钮事件 ----

    def _on_toggle_clicked(self):
        """开始/停止按钮：按当前状态发对应信号。"""
        if self._is_recording:
            self.recording_stopped.emit()
        else:
            self.recording_started.emit()

    # ---- 拖拽 ----

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and not self._drag_offset.isNull():
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_offset = QPoint()
        event.accept()

    # ---- 圆角背景 ----

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        p = get_palette()
        rect = self.rect().adjusted(2, 2, -2, -2)
        painter.setBrush(QBrush(QColor(p.BG_CARD)))
        painter.setPen(QPen(QColor(p.BORDER), 1))
        painter.drawRoundedRect(rect, 12, 12)
