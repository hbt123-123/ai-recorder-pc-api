"""实时声波可视化组件。

录音时由麦克风音量驱动一排中心对称的竖条跳动，给出"正在收音"的直观反馈。
- 音量从 AudioRecorder.current_level() 读取（音频线程写入，GUI 线程读取）。
- 组件自带 25fps 定时器，独立于计时器，保证波形流畅。
- 每根条独立跳动 + 自然衰减，即使音量恒定也有活感。
"""

import logging
import random

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from pc_api.gui.styles import Palette

logger = logging.getLogger(__name__)


class WaveformWidget(QWidget):
    """声波可视化：N 根中心对称竖条，高度由麦克风音量驱动。"""

    BAR_COUNT = 32

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(56)
        self._target = 0.0      # 目标音量（来自麦克风）
        self._level = 0.0       # 平滑后的当前音量
        self._bars = [0.0] * self.BAR_COUNT  # 每根条当前高度（0~1）
        self._level_provider = None  # callable -> float，录音时由外部注入
        self._provider_failed_logged = False  # provider 失败只告警一次的标志

        self._timer = QTimer(self)
        self._timer.setInterval(40)   # ~25fps
        self._timer.timeout.connect(self._tick)
        self.setVisible(False)        # 未录音时不占空间

    def start(self) -> None:
        """录音开始：重置状态并启动跳动。"""
        self._target = 0.0
        self._level = 0.0
        self._bars = [0.0] * self.BAR_COUNT
        self.setVisible(True)
        self._timer.start()

    def stop(self) -> None:
        """录音结束：停止跳动并收起。"""
        self._timer.stop()
        self._level = 0.0
        self._bars = [0.0] * self.BAR_COUNT
        self.update()
        self.setVisible(False)

    def set_level(self, level: float) -> None:
        """直接设置当前麦克风音量（0.0~1.0）。"""
        self._target = max(0.0, min(1.0, level))

    def set_level_provider(self, provider) -> None:
        """注入音量来源（callable，返回 0.0~1.0）。

        设置后波形组件在每帧自动调用 provider 读取最新音量，
        无需外部定时喂值，保证波形流畅。
        """
        self._level_provider = provider

    def _tick(self) -> None:
        # 从 provider 读取最新音量（若已注入）
        if self._level_provider is not None:
            try:
                self._target = max(0.0, min(1.0, float(self._level_provider())))
            except Exception as exc:
                # 首次失败告警一次（含异常摘要），之后静默避免每帧刷屏
                if not self._provider_failed_logged:
                    logger.warning("读取音量 provider 失败，此后静默：%s", exc)
                    self._provider_failed_logged = True
            else:
                self._provider_failed_logged = False  # 成功时复位标志
        # 音量平滑趋近目标，避免数字跳变
        self._level += (self._target - self._level) * 0.45
        center = (self.BAR_COUNT - 1) / 2
        for i in range(self.BAR_COUNT):
            # 中心权重：中间条跳得更高，两侧渐低
            dist = abs(i - center) / center if center > 0 else 0
            weight = 1.0 - dist * 0.5
            # 每根条独立抖动，让波形有"活感"
            jitter = 0.6 + 0.8 * random.random()
            target_h = self._level * weight * jitter
            self._bars[i] += (target_h - self._bars[i]) * 0.6
            self._bars[i] *= 0.94   # 自然衰减
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        w = self.width()
        h = self.height()
        mid = h / 2
        gap = 2
        bar_w = (w - gap * (self.BAR_COUNT - 1)) / self.BAR_COUNT
        radius = min(bar_w / 2, 3)

        for i, v in enumerate(self._bars):
            v = max(0.0, min(1.0, v))
            bar_h = max(3.0, v * (h * 0.9))
            x = i * (bar_w + gap)
            rect = QRectF(x, mid - bar_h / 2, bar_w, bar_h)
            # 透明度随高度变化：矮条淡，高条实
            color = QColor(Palette.PRIMARY)
            color.setAlpha(int(70 + v * 185))
            painter.setBrush(color)
            painter.drawRoundedRect(rect, radius, radius)
