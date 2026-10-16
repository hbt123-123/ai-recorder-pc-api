"""屏幕截图封装（Qt 原生 QScreen.grabWindow）。

视频会议模式下，截取屏幕内容（共享屏幕、PPT、白板）作为图片附件，
走现有 RecordingService.add_attachment 链路 → VLM 描述 → 附件展示。

两种截屏方式：
- ``capture_full``：截取指定显示器全屏（默认主屏）
- ``capture_region``：弹出半透明遮罩，用户拖拽框选区域

接口与 ``pc_api.camera.CameraCapture.capture()`` 对齐
（输入 output_path，返回路径，失败抛 RuntimeError）。

依赖：PySide6（已为 GUI 依赖，无新增）。
"""

import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class ScreenCapture:
    """屏幕截图：全屏 / 框选。"""

    def __init__(self, default_screen_index: int = 0):
        self.default_screen_index = default_screen_index

    def capture_full(self, output_path: str,
                     screen_index: Optional[int] = None) -> str:
        """截取指定显示器全屏，保存为 jpg。

        Args:
            output_path: 输出图片路径
            screen_index: 显示器序号；None 用 default_screen_index

        Returns:
            output_path（与输入一致）

        Raises:
            RuntimeError: 无可用显示器或保存失败
        """
        from PySide6.QtGui import QGuiApplication

        idx = screen_index if screen_index is not None else self.default_screen_index
        screens = QGuiApplication.screens()
        if not screens:
            raise RuntimeError("无可用显示器")
        if idx < 0 or idx >= len(screens):
            logger.warning(
                f"screen_index {idx} 越界（共 {len(screens)} 屏），回退主屏"
            )
            idx = 0

        screen = screens[idx]
        pixmap = screen.grabWindow(0)  # 0 = 整个屏幕
        if pixmap.isNull():
            raise RuntimeError(f"截屏失败：screen={screen.name()}")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        ok = pixmap.save(output_path, "JPG", 90)
        if not ok:
            raise RuntimeError(f"图片保存失败: {output_path}")
        logger.info(f"全屏截屏: screen={screen.name()} → {output_path}")
        return output_path

    def capture_region(self, output_path: str, parent=None) -> Optional[str]:
        """弹出半透明遮罩，用户拖拽框选区域后截屏。

        交互：鼠标按下→拖拽选区→释放→回车/双击确认；Esc 取消。
        确认后截取主屏全屏再按选区裁剪保存。

        Args:
            output_path: 输出图片路径
            parent: 父窗口（用于模态遮罩）

        Returns:
            output_path（确认）；None（用户取消）
        """
        overlay = _RegionSelectorOverlay(parent)
        rect = overlay.exec_and_get_rect()
        if rect is None:
            logger.info("框选截屏已取消")
            return None

        # 找到完整包含选区的显示器。原实现总是截主屏，且框选坐标没有
        # 加上虚拟桌面偏移，多屏会议时会截错或直接裁剪失败。
        from PySide6.QtGui import QGuiApplication, QPixmap
        from PySide6.QtCore import QRect

        screens = QGuiApplication.screens()
        if not screens:
            raise RuntimeError("无可用显示器")
        screen = next((s for s in screens if s.geometry().contains(rect)), None)
        if screen is None:
            raise RuntimeError("框选区域跨越多个显示器，请在单个显示器内完成框选")
        full = screen.grabWindow(0)
        if full.isNull():
            raise RuntimeError("截屏失败")

        # rect 是虚拟桌面全局坐标，需转为相对目标显示器的坐标
        geo = screen.geometry()
        local_rect = QRect(
            rect.x() - geo.x(), rect.y() - geo.y(),
            rect.width(), rect.height()
        )
        cropped = full.copy(local_rect)
        if cropped.isNull():
            raise RuntimeError(f"选区裁剪失败: {local_rect}")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        ok = cropped.save(output_path, "JPG", 90)
        if not ok:
            raise RuntimeError(f"图片保存失败: {output_path}")
        logger.info(f"框选截屏: rect={local_rect} → {output_path}")
        return output_path

    @staticmethod
    def list_screens() -> List[dict]:
        """列出所有可用显示器。"""
        try:
            from PySide6.QtGui import QGuiApplication
            result = []
            for i, s in enumerate(QGuiApplication.screens()):
                geo = s.geometry()
                result.append({
                    "index": i,
                    "name": s.name(),
                    "geometry": (geo.x(), geo.y(), geo.width(), geo.height()),
                })
            return result
        except Exception:
            return []

    @staticmethod
    def available() -> bool:
        """是否有可用显示器。"""
        try:
            from PySide6.QtGui import QGuiApplication
            return len(QGuiApplication.screens()) > 0
        except Exception:
            return False


class _RegionSelectorOverlay:
    """框选遮罩：半透明全屏置顶 + QRubberBand 选区。

    用法：``rect = _RegionSelectorOverlay(parent).exec_and_get_rect()``
    返回 QRect 或 None（取消）。

    实现为模态对话框式：内部 QEventLoop 阻塞直到用户确认/取消。
    """

    def __init__(self, parent=None):
        self._parent = parent
        self._rect = None

    def exec_and_get_rect(self):
        """阻塞执行，返回选区 QRect 或 None。"""
        from PySide6.QtCore import Qt, QEventLoop, QPoint, QRect, Signal, QObject
        from PySide6.QtGui import QBrush, QColor, QFont, QPainter
        from PySide6.QtWidgets import (
            QWidget, QRubberBand, QApplication,
        )

        # 用一个全屏置顶半透明 widget 接收鼠标事件
        screens = QApplication.screens()
        if not screens:
            return None
        # 覆盖虚拟桌面全区域（多屏拼接）
        virtual_geo = screens[0].virtualGeometry()

        overlay = QWidget()
        overlay.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        overlay.setGeometry(virtual_geo)
        overlay.setCursor(Qt.CursorShape.CrossCursor)

        rubber = QRubberBand(QRubberBand.Shape.Rectangle, overlay)

        origin = QPoint()

        class _Signals(QObject):
            done = Signal()

        signals = _Signals()
        loop = QEventLoop()

        def on_mouse_press(event):
            nonlocal origin
            if event.button() == Qt.MouseButton.LeftButton:
                origin = event.position().toPoint()
                rubber.setGeometry(QRect(origin, origin))
                rubber.show()

        def on_mouse_move(event):
            if event.buttons() & Qt.MouseButton.LeftButton and rubber.isVisible():
                end = event.position().toPoint()
                rubber.setGeometry(QRect(origin, end).normalized())

        def on_mouse_release(event):
            if event.button() == Qt.MouseButton.LeftButton and rubber.isVisible():
                local = rubber.geometry()
                self._rect = local.translated(overlay.geometry().topLeft())
                # 不立即结束，等回车/双击确认（用户可调整选区）

        def on_key_press(event):
            if event.key() == Qt.Key.Key_Escape:
                self._rect = None
                signals.done.emit()
            elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if rubber.isVisible():
                    signals.done.emit()
                else:
                    self._rect = None
                    signals.done.emit()

        def on_double_click(event):
            if rubber.isVisible():
                local = rubber.geometry()
                self._rect = local.translated(overlay.geometry().topLeft())
                signals.done.emit()

        # Alt+F4 / WM 关闭遮罩窗口也必须退出事件循环：否则 loop.exec() 在
        # 主线程永久阻塞，整个应用假死只能强杀。按取消处理（rect=None）。
        def on_close_event(event):
            self._rect = None
            event.accept()
            signals.done.emit()

        def on_destroyed(*_args):
            signals.done.emit()

        # 重写 paintEvent 画半透明遮罩 + 屏幕共享提示
        def paint_event(event):
            painter = QPainter(overlay)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # 半透明黑色遮罩
            painter.fillRect(overlay.rect(), QColor(0, 0, 0, 100))
            # 顶部提示:屏幕共享时框选操作对参会人可见(遮罩本身是全屏置顶窗口,
            # 屏幕共享会捕获到它)。黄色醒目文字提醒用户。
            painter.setPen(QColor("#FFCC00"))
            font = painter.font()
            font.setPointSize(12)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(
                overlay.rect().adjusted(0, 24, 0, 0),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                "⚠ 屏幕共享时此操作对参会人可见",
            )
            painter.end()

        overlay.paintEvent = paint_event
        overlay.mousePressEvent = on_mouse_press
        overlay.mouseMoveEvent = on_mouse_move
        overlay.mouseReleaseEvent = on_mouse_release
        overlay.mouseDoubleClickEvent = on_double_click
        overlay.keyPressEvent = on_key_press
        overlay.closeEvent = on_close_event

        signals.done.connect(loop.quit)
        # 窗口被外部强制销毁（绕过 closeEvent）时兜底退出循环
        overlay.destroyed.connect(on_destroyed)

        overlay.show()
        overlay.activateWindow()
        overlay.setFocus()

        loop.exec()

        # 先取结果再关窗口：close() 会同步触发 on_close_event（其职责是
        # 把"未确认即关闭"标记为取消），若先关后取会把已确认的选区清空
        result = self._rect
        overlay.close()
        overlay.deleteLater()

        return result
