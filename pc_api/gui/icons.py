"""SVG 图标系统（Lucide 风格路径渲染）。

用 QPainterPath 定义图标路径，QIcon/QPixmap 按需渲染。
避免依赖外部 SVG 文件或 emoji 字体，跨平台一致。

用法:
    from pc_api.gui.icons import Icon, icon_size

    btn = QPushButton()
    btn.setIcon(Icon("record").to_qicon())
    lbl = QLabel()
    lbl.setPixmap(Icon("settings").to_pixmap(20, 20))
"""

from typing import Dict

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QPainterPath, QPen, QBrush,
    QColor,
)

from pc_api.gui.styles import get_palette


# ============================================================
# 图标路径定义（Lucide 风格：24x24 viewBox，stroke-based）
# ============================================================

def _circle(cx: float, cy: float, r: float) -> QPainterPath:
    p = QPainterPath()
    p.addEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
    return p


def _rect(x: float, y: float, w: float, h: float) -> QPainterPath:
    p = QPainterPath()
    p.addRect(QRectF(x, y, w, h))
    return p


def _rounded_rect(x: float, y: float, w: float, h: float, r: float) -> QPainterPath:
    p = QPainterPath()
    p.addRoundedRect(QRectF(x, y, w, h), r, r)
    return p


def _polygon(points) -> QPainterPath:
    p = QPainterPath()
    p.moveTo(QPointF(*points[0]))
    for pt in points[1:]:
        p.lineTo(QPointF(*pt))
    p.closeSubpath()
    return p


def _line(x1: float, y1: float, x2: float, y2: float) -> QPainterPath:
    p = QPainterPath()
    p.moveTo(QPointF(x1, y1))
    p.lineTo(QPointF(x2, y2))
    return p


def _path(d: str) -> QPainterPath:
    """简易 SVG path 解析（仅支持 M/L/C/Z 命令，兼容 "M3 6 L21 6" 连写格式）。"""
    p = QPainterPath()
    tokens = d.replace(",", " ").split()
    argc = {"M": 2, "L": 2, "C": 6, "Z": 0}
    i = 0
    n = len(tokens)
    while i < n:
        head = tokens[i]
        cmd = head[0].upper()
        i += 1
        need = argc.get(cmd)
        if need is None:
            continue  # 未知命令，跳过
        # 命令字符后的剩余部分是第一个数值，其余按需消费纯数字 token
        nums = []
        rest = head[1:]
        if rest:
            try:
                nums.append(float(rest))
            except ValueError:
                continue  # 首个数值非法，跳过该命令
        while len(nums) < need and i < n:
            try:
                nums.append(float(tokens[i]))
                i += 1
            except ValueError:
                break  # 遇到非数字（下一个命令），停止消费
        if len(nums) < need:
            continue  # 数字个数不足，防御性跳过该命令
        if cmd == "M":
            p.moveTo(nums[0], nums[1])
        elif cmd == "L":
            p.lineTo(nums[0], nums[1])
        elif cmd == "C":
            p.cubicTo(nums[0], nums[1], nums[2], nums[3], nums[4], nums[5])
        elif cmd == "Z":
            p.closeSubpath()
    return p


# 图标名 -> (path 函数, fill 或 stroke)
_ICONS: Dict[str, QPainterPath] = {}


def _register(name: str, path: QPainterPath):
    _ICONS[name] = path


# ---- 录音/播放 ----
_register("record", _circle(12, 12, 6))  # 实心圆，录音
_register("stop", _rounded_rect(6, 6, 12, 12, 1))  # 方块，停止
_register("play", _polygon([(8, 5), (19, 12), (8, 19)]))  # 三角形，播放
_register("pause", _path("M8 5 L10 5 L10 19 L8 19 Z M14 5 L16 5 L16 19 L14 19 Z"))

# ---- 设备/功能 ----
_register("camera", _path("M3 7 L3 19 L21 19 L21 9 L14 9 L12 6 L7 6 L5 8 L3 8 Z") + _circle(12, 13, 3.5))
_register("screenshot", _path("M3 6 L3 9 M3 6 L6 6 M21 6 L21 9 M21 6 L18 6 M3 18 L3 15 M3 18 L6 18 M21 18 L21 15 M21 18 L18 18") + _rect(8, 8, 8, 8))
_register("float", _path("M3 3 L21 3 L21 14 L14 14 L14 21 L3 21 Z"))  # 悬浮窗
_register("expand", _path("M8 3 L3 3 L3 8 M16 3 L21 3 L21 8 M8 21 L3 21 L3 16 M16 21 L21 21 L21 16"))  # 展开
_register("settings", _circle(12, 12, 3) + _path("M12 2 L12 5 M12 19 L12 22 M2 12 L5 12 M19 12 L22 12 M4.9 4.9 L7 7 M17 17 L19.1 19.1 M4.9 19.1 L7 17 M17 7 L19.1 4.9"))

# ---- 文件/操作 ----
_register("file", _path("M6 2 L14 2 L14 8 L20 8 L20 22 L6 22 Z") + _path("M14 2 L14 8 L20 8"))
_register("plus", _path("M12 5 L12 19 M5 12 L19 12"))
_register("search", _circle(11, 11, 7) + _line(16.5, 16.5, 21, 21))
_register("edit", _path("M4 20 L4 16 L16 4 L20 8 L8 20 Z"))
_register("copy", _rect(8, 8, 12, 12) + _path("M4 16 L4 4 L16 4"))
_register("trash", _path("M4 7 L4 5 L9 5 L10 3 L14 3 L15 5 L20 5 L20 7 M6 7 L18 7 L17 21 L7 21 Z"))
_register("folder", _path("M3 7 L3 19 L21 19 L21 9 L11 9 L9 7 Z"))

# ---- 导航 ----
_register("menu", _path("M3 6 L21 6 M3 12 L21 12 M3 18 L21 18"))  # 汉堡
_register("chevron_left", _path("M15 6 L9 12 L15 18"))
_register("chevron_right", _path("M9 6 L15 12 L9 18"))
_register("chevron_down", _path("M6 9 L12 15 L18 9"))

# ---- 状态 ----
_register("check", _path("M5 12 L10 17 L19 7"))
_register("alert", _path("M12 3 L2 20 L22 20 Z") + _line(12, 9, 12, 14) + _circle(12, 17, 0.5))
_register("clock", _circle(12, 12, 9) + _path("M12 7 L12 12 L16 14"))
_register("refresh", _path(
    # 上半弧（C 近似）+ 右上角箭头折线
    "M3 12 C3 7 7 3 12 3 L16 3 "
    "M21 3 L21 8 M21 3 L16 3 "
    # 下半弧（C 近似）+ 左下角箭头折线
    "M21 12 C21 17 17 21 12 21 L8 21 "
    "M3 21 L3 16 M3 21 L8 21"
))

# ---- tier 图标 ----
# 注：解析器仅支持 M/L/C/Z，弧线一律用三次贝塞尔（C）近似
_register("mic", _path(
    # 胶囊形话筒头（上下半圆用 C 近似）
    "M12 2 C10.5 2 9 3.3 9 5 L9 11 "
    "C9 12.7 10.3 14 12 14 "
    "C13.7 14 15 12.7 15 11 "
    "L15 5 C15 3.3 13.5 2 12 2"
) + _path(
    # 底部弧形支架 + 立杆
    "M5 11 C5 15.4 8.1 19 12 19 C15.9 19 19 15.4 19 11 "
    "M12 19 L12 22"
))
_register("zap", _path("M13 2 L3 14 L12 14 L11 22 L21 10 L12 10 Z"))  # 闪电=实时
_register("gem", _path("M6 3 L18 3 L22 9 L12 22 L2 9 Z") + _path("M2 9 L22 9 M12 3 L8 9 L12 22 M12 3 L16 9"))  # 钻石=专业
_register("crown", _path("M3 7 L8 12 L12 5 L16 12 L21 7 L19 19 L5 19 Z"))  # 皇冠=旗舰

# ---- 主题 ----
_register("sun", _circle(12, 12, 4) + _path("M12 2 L12 4 M12 20 L12 22 M2 12 L4 12 M20 12 L22 12 M4.9 4.9 L6.3 6.3 M17.7 17.7 L19.1 19.1 M4.9 19.1 L6.3 17.7 M17.7 6.3 L19.1 4.9"))
_register("moon", _path(
    "M21 12.8 C21 17.7 17 21.8 12 21.8 "
    "C9.8 21.8 7.7 21 6.1 19.7 "
    "C13 19.2 18.3 13.6 18.6 6.6 "
    "C20.1 8.3 21 10.5 21 12.8 Z"
))

# ---- 音量 ----
_register("volume", _path("M11 5 L6 9 L2 9 L2 15 L6 15 L11 19 Z") + _path(
    # 两道声波弧（C 近似，向右鼓出）
    "M15.5 8.5 C17.6 10.1 17.6 13.9 15.5 15.5 "
    "M19 5 C22.2 7.6 22.2 16.4 19 19"
))

# ---- 笔记 ----
_register("note", _path("M4 4 L4 20 L20 20 L20 8 L16 4 Z") + _path("M16 4 L16 8 L20 8") + _path("M8 13 L16 13 M8 17 L13 17"))
_register("image", _path("M3 5 L3 19 L21 19 L21 5 Z") + _circle(9, 10, 2) + _path("M3 17 L9 11 L14 16 L17 13 L21 17"))
_register("text", _path("M4 6 L4 5 L20 5 L20 6 M12 5 L12 19 M9 19 L15 19"))


# ============================================================
# 图标渲染器
# ============================================================

class Icon:
    """图标渲染器：按名称渲染为 QPixmap / QIcon。

    所有图标基于 24x24 viewBox，缩放到任意尺寸。
    stroke 风格（Lucide 风格）：线条图标，用 QPen 绘制。
    fill 风格：实心图标（如 record/stop/play），用 QBrush 填充。
    """

    # fill 类型图标（实心绘制）
    _FILL_ICONS = frozenset({"record", "stop", "play", "pause", "check", "gem", "crown", "zap"})

    def __init__(self, name: str, color: str = None):
        self.name = name
        self._color = color  # None 时用主题 TEXT_PRIMARY

    def to_pixmap(self, width: int = 20, height: int = 20) -> QPixmap:
        """渲染为 QPixmap。"""
        path = _ICONS.get(self.name)
        if path is None:
            pm = QPixmap(width, height)
            pm.fill(Qt.GlobalColor.transparent)
            return pm

        pm = QPixmap(width, height)
        pm.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # 颜色
        color = self._color or get_palette().TEXT_PRIMARY
        qcolor = QColor(color)

        # 缩放路径到目标尺寸
        # 原始 viewBox 24x24，加 2px margin
        scale = min(width, height) / 24.0
        painter.scale(scale, scale)

        if self.name in self._FILL_ICONS:
            painter.fillPath(path, QBrush(qcolor))
        else:
            # stroke 风格：线条图标
            pen = QPen(qcolor)
            pen.setWidth(2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        painter.end()
        return pm

    def to_qicon(self, width: int = 20, height: int = 20) -> QIcon:
        """渲染为 QIcon。"""
        return QIcon(self.to_pixmap(width, height))

    def to_pixmap_colored(self, color: str, width: int = 20, height: int = 20) -> QPixmap:
        """用指定颜色渲染。"""
        old = self._color
        self._color = color
        pm = self.to_pixmap(width, height)
        self._color = old
        return pm


def make_icon(name: str, size: int = 20, color: str = None) -> QIcon:
    """便捷函数：直接生成 QIcon。"""
    return Icon(name, color).to_qicon(size, size)


def icon_pixmap(name: str, size: int = 20, color: str = None) -> QPixmap:
    """便捷函数：直接生成 QPixmap。"""
    return Icon(name, color).to_pixmap(size, size)
