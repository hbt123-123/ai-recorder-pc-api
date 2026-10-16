"""首启引导页：第一次打开应用时展示，帮助用户选择 ASR tier。

卡片式三页引导：欢迎 -> 选择转写档位 -> 完成。
模态 QDialog，exec() 返回 Accepted / Rejected。
通过 get_selected_tier() 获取用户选择的 tier_id。
"""

import logging

from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QPainter, QColor, QBrush
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from core.transcription.tiers import list_tiers, TIER_ORDER
from pc_api.gui.icons import Icon
from pc_api.gui.styles import Metrics, get_palette, fade_in

logger = logging.getLogger(__name__)

_TIER_ICON = {
    "free": "mic",
    "realtime": "zap",
    "pro_lite": "gem",
    "pro_pro": "crown",
}


def _tier_accent_color(tier_id: str) -> str:
    p = get_palette()
    return {
        "free": p.ACCENT_FREE,
        "realtime": p.ACCENT_REALTIME,
        "pro_lite": p.ACCENT_PRO_LITE,
        "pro_pro": p.ACCENT_PRO_PRO,
    }.get(tier_id, p.PRIMARY)


# ============================================================
# 进度指示点（3 圆点，显示当前步骤）
# ============================================================

class _ProgressDots(QWidget):
    """三步骤进度指示器：当前步高亮，已完成步变绿。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current = 0
        self.setFixedHeight(12)

    def set_current(self, idx: int, total: int = 3):
        self._current = idx
        self._total = total
        self.update()

    def paintEvent(self, event):
        p = get_palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        total = getattr(self, '_total', 3)
        spacing = 12
        dot_w = 28
        total_w = dot_w * total + spacing * (total - 1)
        start_x = (w - total_w) / 2

        for i in range(total):
            x = start_x + i * (dot_w + spacing)
            is_active = (i == self._current)
            is_done = (i < self._current)

            if is_done:
                color = QColor(p.SUCCESS)
                rw = dot_w
            elif is_active:
                color = QColor(p.PRIMARY)
                rw = dot_w + 6
            else:
                color = QColor(p.BORDER)
                rw = dot_w

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color))
            rect = QRectF(x, 3, rw, 6)
            painter.drawRoundedRect(rect, 3, 3)


# ============================================================
# Tier 选择卡片（带顶色条、图标背景、悬停高亮）
# ============================================================

class _TierCard(QFrame):
    """单档 tier 选择卡片，点击选中并发出信号。"""

    clicked = Signal(str)

    def __init__(self, tier, parent=None):
        super().__init__(parent)
        self.setObjectName("OnboardingCard")
        self.tier_id = tier.tier_id
        self._selected = False
        self._accent_color = _tier_accent_color(tier.tier_id)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("selected", "false")
        self._build_ui(tier)

    def _build_ui(self, tier):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部彩色条
        accent = QFrame()
        accent.setObjectName("tierAccentTop")
        accent.setProperty("accent", self.tier_id)
        accent.setFixedHeight(4)
        layout.addWidget(accent)

        # 内容区
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(
            Metrics.SPACING_MD, Metrics.SPACING_MD,
            Metrics.SPACING_MD, Metrics.SPACING_MD,
        )
        content_layout.setSpacing(Metrics.SPACING_SM)

        # 图标 + 名称行
        top_row = QHBoxLayout()
        top_row.setSpacing(Metrics.SPACING_SM)

        icon_wrap = QLabel()
        icon_wrap.setObjectName("tierIconWrap")
        icon_wrap.setFixedSize(48, 48)
        icon_name = _TIER_ICON.get(tier.tier_id, "mic")
        icon_pix = Icon(icon_name, self._accent_color).to_pixmap(28, 28)
        icon_wrap.setPixmap(icon_pix)
        icon_wrap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_row.addWidget(icon_wrap, 0, Qt.AlignmentFlag.AlignTop)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        name = QLabel(tier.label)
        name.setObjectName("cardTitle")
        title_col.addWidget(name)

        price = QLabel(tier.price)
        price.setObjectName("tierPriceTag")
        title_col.addWidget(price)

        top_row.addLayout(title_col, 1)
        content_layout.addLayout(top_row)

        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {get_palette().DIVIDER};")
        line.setFixedHeight(1)
        content_layout.addWidget(line)

        # 特点描述
        desc = QLabel(tier.description)
        desc.setObjectName("tierFeatureTag")
        desc.setWordWrap(True)
        content_layout.addWidget(desc, 1)

        layout.addWidget(content, 1)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.tier_id)
        super().mousePressEvent(event)

    def set_selected(self, selected: bool):
        self._selected = selected
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def is_selected(self) -> bool:
        return self._selected


# ============================================================
# 引导对话框
# ============================================================

class OnboardingDialog(QDialog):
    """首启引导对话框：三页卡片式引导，选择 ASR tier。"""

    tier_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("欢迎使用 AI 录音")
        self.setFixedSize(640, 500)
        self._selected_tier: str = ""
        self._cards: dict = {}
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(0)
        outer.setContentsMargins(0, 0, 0, 0)

        # 步骤指示
        self._progress = _ProgressDots()
        outer.addWidget(self._progress, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addSpacing(Metrics.SPACING_SM)

        # 页面栈
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_welcome_page())
        self.stack.addWidget(self._build_tier_page())
        self.stack.addWidget(self._build_done_page())
        outer.addWidget(self.stack, 1)

        # 底部按钮栏
        outer.addLayout(self._build_bottom_bar())

        self._progress.set_current(0)

    # ---- 页面构建 ----

    def _build_welcome_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(
            Metrics.SPACING_XL, Metrics.SPACING_LG,
            Metrics.SPACING_XL, Metrics.SPACING_MD,
        )
        layout.setSpacing(Metrics.SPACING_MD)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        p = get_palette()

        # Hero 图标（大尺寸 + 圆形背景）
        hero = QLabel()
        hero.setObjectName("heroCircle")
        hero.setFixedSize(72, 72)
        hero.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_icon = Icon("mic", p.PRIMARY).to_pixmap(40, 40)
        hero.setPixmap(hero_icon)
        layout.addWidget(hero, 0, Qt.AlignmentFlag.AlignHCenter)

        # 步骤标签
        step = QLabel("第 1 步 / 共 3 步")
        step.setObjectName("onboardingStep")
        step.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(step)

        # 标题
        title = QLabel("欢迎使用 AI 录音")
        title.setObjectName("onboardingTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # 副标题
        subtitle = QLabel("选择最适合你的 AI 转写方案")
        subtitle.setObjectName("onboardingSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        # 说明
        desc = QLabel(
            "AI 录音提供四档转写方案，从免费到专业，"
            "满足不同场景的语音转文字需求。\n"
            "接下来请选择一个适合你的转写档位。"
        )
        desc.setObjectName("onboardingDesc")
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(desc)

        layout.addStretch()
        return page

    def _build_tier_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(
            Metrics.SPACING_XL, Metrics.SPACING_MD,
            Metrics.SPACING_XL, Metrics.SPACING_MD,
        )
        layout.setSpacing(Metrics.SPACING_MD)

        # 标题区
        header = QVBoxLayout()
        header.setSpacing(4)

        step = QLabel("第 2 步 / 共 3 步")
        step.setObjectName("onboardingStep")
        step.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(step)

        title = QLabel("选择转写档位")
        title.setObjectName("onboardingTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(title)

        subtitle = QLabel("从免费体验到专业品质，选择你需要的方案")
        subtitle.setObjectName("onboardingSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(subtitle)

        layout.addLayout(header)

        # 2×2 卡片网格
        grid = QGridLayout()
        grid.setSpacing(Metrics.SPACING_MD)
        for i, tier in enumerate(list_tiers()):
            card = _TierCard(tier)
            card.clicked.connect(self._on_card_clicked)
            self._cards[tier.tier_id] = card
            row, col = divmod(i, 2)
            grid.addWidget(card, row, col)
        layout.addLayout(grid, 1)
        return page

    def _build_done_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(
            Metrics.SPACING_XL, Metrics.SPACING_LG,
            Metrics.SPACING_XL, Metrics.SPACING_MD,
        )
        layout.setSpacing(Metrics.SPACING_MD)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        p = get_palette()

        # 完成图标（绿色圆形背景）
        check = QLabel()
        check.setObjectName("checkCircle")
        check.setFixedSize(72, 72)
        check.setAlignment(Qt.AlignmentFlag.AlignCenter)
        check_icon = Icon("check", p.SUCCESS).to_pixmap(40, 40)
        check.setPixmap(check_icon)
        layout.addWidget(check, 0, Qt.AlignmentFlag.AlignHCenter)

        step = QLabel("第 3 步 / 共 3 步")
        step.setObjectName("onboardingStep")
        step.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(step)

        title = QLabel("一切就绪")
        title.setObjectName("onboardingTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("你可以开始录音了")
        subtitle.setObjectName("onboardingSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        desc = QLabel(
            "随时在设置中切换转写档位，\n"
            "享受 AI 录音带来的高效体验。"
        )
        desc.setObjectName("onboardingDesc")
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(desc)

        layout.addStretch()
        return page

    def _build_bottom_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(
            Metrics.SPACING_XL, Metrics.SPACING_SM,
            Metrics.SPACING_XL, Metrics.SPACING_LG,
        )
        bar.setSpacing(Metrics.SPACING_SM)

        self.btn_prev = QPushButton("上一步")
        self.btn_prev.setObjectName("ghostButton")
        self.btn_prev.clicked.connect(self._on_prev)
        bar.addWidget(self.btn_prev)

        bar.addStretch()

        self.btn_next = QPushButton("下一步")
        self.btn_next.setObjectName("primaryButton")
        self.btn_next.setMinimumWidth(120)
        self.btn_next.clicked.connect(self._on_next)
        bar.addWidget(self.btn_next)

        self._update_buttons()
        return bar

    # ---- 导航 ----

    def _on_prev(self):
        idx = self.stack.currentIndex()
        if idx > 0:
            self.stack.setCurrentIndex(idx - 1)
            self._progress.set_current(idx - 1)
            self._update_buttons()
            self._fade_current()

    def _on_next(self):
        idx = self.stack.currentIndex()
        if idx < self.stack.count() - 1:
            self.stack.setCurrentIndex(idx + 1)
            self._progress.set_current(idx + 1)
            self._update_buttons()
            self._fade_current()
        else:
            self.accept()

    def _fade_current(self):
        """给当前页面加淡入效果。"""
        current = self.stack.currentWidget()
        if current:
            fade_in(current, duration=250)

    def _update_buttons(self):
        idx = self.stack.currentIndex()
        total = self.stack.count()
        self.btn_prev.setVisible(idx > 0)
        self.btn_next.setText("开始使用" if idx == total - 1 else "下一步")
        self.btn_next.setEnabled(idx != 1 or bool(self._selected_tier))

    # ---- tier 选择 ----

    def _on_card_clicked(self, tier_id: str):
        for tid, card in self._cards.items():
            card.set_selected(tid == tier_id)
        self._selected_tier = tier_id
        self.tier_selected.emit(tier_id)
        self._update_buttons()

    def get_selected_tier(self) -> str:
        """返回用户选择的 tier_id；未选择时回退到 free。"""
        return self._selected_tier or TIER_ORDER[0]