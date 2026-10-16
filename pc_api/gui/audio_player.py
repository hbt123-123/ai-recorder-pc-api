"""极简音频播放器组件：录音回放 + 转写文字点击跳转高亮。

用 QMediaPlayer + QAudioOutput 播放音频，进度条驱动转写文字高亮。
"""

import logging

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QPushButton, QSlider, QLabel,
)

from pc_api.gui.styles import Metrics
from pc_api.gui.icons import Icon

logger = logging.getLogger(__name__)


def _format_time(seconds: float) -> str:
    """秒数格式化为 m:ss。"""
    total = int(max(0, seconds))
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


class AudioPlayer(QFrame):
    """极简音频播放器：播放/暂停 + 进度条 + 时间显示。

    信号:
        position_changed(float): 播放位置变化（秒），供转写文字高亮跳转用。
        duration_ready(float): 总时长就绪（秒）。
    """

    position_changed = Signal(float)
    duration_ready = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AudioPlayer")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedHeight(50)

        # 播放器
        self._audio_output = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._duration_ms: int = 0

        # 信号连接
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        # 播放状态变化时刷新按钮图标（自然结束进入 StoppedState 后恢复「播放」图标）
        self._player.playbackStateChanged.connect(self._update_play_icon)

        self._build_ui()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Metrics.SPACING_SM, Metrics.SPACING_XS,
            Metrics.SPACING_SM, Metrics.SPACING_XS,
        )
        layout.setSpacing(Metrics.SPACING_SM)

        # 播放/暂停按钮
        self.btn_play = QPushButton(self)
        self.btn_play.setObjectName("toolButton")
        self.btn_play.setFixedSize(32, 32)
        self.btn_play.setIcon(Icon("play").to_qicon(18, 18))
        self.btn_play.clicked.connect(self._toggle_play)
        layout.addWidget(self.btn_play)

        # 当前时间
        self.lbl_current = QLabel("0:00", self)
        self.lbl_current.setFixedWidth(34)
        self.lbl_current.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.lbl_current)

        # 进度条
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setObjectName("audioSeek")
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(self._on_slider_moved)
        layout.addWidget(self.slider, 1)

        # 总时长
        self.lbl_total = QLabel("0:00", self)
        self.lbl_total.setFixedWidth(34)
        layout.addWidget(self.lbl_total)

    # ------------------------------------------------------------------
    # QMediaPlayer 信号处理
    # ------------------------------------------------------------------
    def _on_position_changed(self, position_ms: int) -> None:
        """播放位置变化 -> 更新进度条、时间标签、发出信号。"""
        self.slider.blockSignals(True)
        self.slider.setValue(position_ms)
        self.slider.blockSignals(False)

        seconds = position_ms / 1000.0
        self.lbl_current.setText(_format_time(seconds))
        self.position_changed.emit(seconds)

    def _on_duration_changed(self, duration_ms: int) -> None:
        """总时长就绪 -> 设置进度条范围、更新总时长标签、发出信号。"""
        self._duration_ms = duration_ms
        self.slider.setRange(0, duration_ms)
        seconds = duration_ms / 1000.0
        self.lbl_total.setText(_format_time(seconds))
        if duration_ms > 0:
            self.duration_ready.emit(seconds)

    # ------------------------------------------------------------------
    # 用户交互
    # ------------------------------------------------------------------
    def _toggle_play(self) -> None:
        """播放/暂停切换。"""
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.pause()
        else:
            self.play()

    def _on_slider_moved(self, value: int) -> None:
        """用户拖动进度条 -> 跳转。"""
        self._player.setPosition(value)
        self.lbl_current.setText(_format_time(value / 1000.0))

    def _update_play_icon(self) -> None:
        """根据播放状态切换图标。"""
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.btn_play.setIcon(Icon("pause").to_qicon(18, 18))
        else:
            self.btn_play.setIcon(Icon("play").to_qicon(18, 18))

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def load_audio(self, path: str) -> None:
        """加载音频文件。"""
        self._player.stop()
        # Windows 路径需用 QUrl.fromLocalFile，否则会被解析成 "c:" 协议
        self._player.setSource(QUrl.fromLocalFile(path) if path else QUrl())
        self.slider.setValue(0)
        self.lbl_current.setText("0:00")
        self._update_play_icon()
        logger.debug("AudioPlayer loaded: %s", path)

    def play(self) -> None:
        """播放。"""
        self._player.play()
        self._update_play_icon()

    def pause(self) -> None:
        """暂停。"""
        self._player.pause()
        self._update_play_icon()

    def stop(self) -> None:
        """停止。"""
        self._player.stop()
        self.slider.setValue(0)
        self.lbl_current.setText("0:00")
        self._update_play_icon()

    def seek_to(self, seconds: float) -> None:
        """跳转到指定秒数。"""
        position_ms = int(seconds * 1000)
        self._player.setPosition(position_ms)
        self.slider.setValue(position_ms)
        self.lbl_current.setText(_format_time(seconds))

    def is_playing(self) -> bool:
        """是否正在播放。"""
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
