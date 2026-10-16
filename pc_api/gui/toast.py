"""Toast 通知组件：非侵入式底部弹出提示。

用于替代状态栏的临时消息，3 秒自动消失，带淡入淡出动画。
适合「转写完成」「段 2 已转写」等非阻塞通知。

用法:
    Toast.show(parent_widget, "转写完成")
    Toast.show(parent_widget, "段 3 转写完成", duration=2000)
"""

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QWidget, QLabel, QHBoxLayout, QFrame, QGraphicsOpacityEffect,
)

from pc_api.gui.styles import Metrics


class Toast(QFrame):
    """单条 Toast 通知。

    底部居中悬浮显示，自动淡入 -> 停留 -> 淡出 -> 销毁。
    """

    _instances: list = []  # 防止 GC

    def __init__(self, parent: QWidget, text: str, duration: int = 3000):
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Metrics.SPACING_MD, Metrics.SPACING_SM,
            Metrics.SPACING_MD, Metrics.SPACING_SM,
        )
        layout.setSpacing(Metrics.SPACING_SM)

        self.lbl = QLabel(text)
        self.lbl.setObjectName("toastText")
        self.lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl)

        self.adjustSize()

        # 透明度效果
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)

        # 动画（finished 信号只连接一次，用 _phase 状态机驱动，避免重复 disconnect 告警）
        self._fade_anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.finished.connect(self._on_fade_finished)

        # 定位到底部居中
        self._position(parent)

        # 生命周期
        self._duration = duration
        self._phase = "in"  # in -> stay -> out

        # 停留定时器归属 self（单次触发）：随父窗口销毁自动停止，
        # 避免父窗口析构后 singleShot 回调触发 RuntimeError
        self._stay_timer = QTimer(self)
        self._stay_timer.setSingleShot(True)
        self._stay_timer.setInterval(self._duration)
        self._stay_timer.timeout.connect(self._fade_out)

        Toast._instances.append(self)

    def _position(self, parent: QWidget):
        """定位到父窗口底部居中。"""
        pw = parent.width()
        ph = parent.height()
        tw = self.width()
        th = self.height()
        x = (pw - tw) // 2
        y = ph - th - 80  # 底部留 80px 间距（避让状态栏）
        self.move(max(0, x), max(0, y))

    def show_anim(self):
        """播放淡入 -> 停留 -> 淡出动画。"""
        # 注意:类方法 Toast.show 遮蔽了 QWidget.show,这里必须用 super().show()
        super().show()
        self.raise_()

        # 淡入 200ms
        self._phase = "in"
        self._fade_anim.setDuration(200)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.start()

        # 启动停留定时器（重复 show 时 restart，不会叠加多个定时器）
        self._stay_timer.start()

    def _on_fade_finished(self):
        """动画完成回调:按阶段推进 in -> stay -> out。"""
        if self._phase == "in":
            self._phase = "stay"  # 淡出由 _stay_timer 到点触发
        elif self._phase == "out":
            self._destroy()

    def _fade_out(self):
        """淡出 300ms -> 销毁。"""
        if self._phase != "stay":
            return
        self._phase = "out"
        self._fade_anim.setDuration(300)
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()

    def _destroy(self):
        """销毁 Toast（幂等：淡出结束与窗口销毁可能双触发）。"""
        self.hide()
        try:
            Toast._instances.remove(self)
        except ValueError:
            pass
        self.deleteLater()

    @classmethod
    def show(cls, parent: QWidget, text: str, duration: int = 3000) -> "Toast":
        """便捷方法：在 parent 上显示一条 Toast。"""
        toast = cls(parent, text, duration)
        toast.show_anim()
        return toast
