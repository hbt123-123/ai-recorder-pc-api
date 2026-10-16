"""顶栏 Tier 分段控件。

水平分段控件(类似 macOS Segmented Control),4 档 tier + 1 个「文件转写」入口。
选中态 teal 背景,未选中态浅灰;点击发出对应信号:
- tier_selected(str): 选中某档 tier(tier_id)
- file_upload_selected(): 选中「文件转写」独立页面
- sidebar_toggle_requested(): 汉堡按钮点击,请求折叠/展开侧边栏
MainWindow 接收信号并切换视图。

固定高度 56px,左对齐控件,右侧留空。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QToolButton, QLabel, QSizePolicy,
    QPushButton,
)

from core.transcription.tiers import TIER_ORDER, get_tier, list_tiers
from pc_api.gui.styles import Metrics, get_palette
from pc_api.gui.icons import make_icon, icon_pixmap


class _TierSegmentButton(QToolButton):
    """单个 tier 分段按钮:垂直两行(图标+标题 + 价格)。"""

    def __init__(self, tier_id: str, parent=None):
        super().__init__(parent)
        self.tier_id = tier_id
        self.t = get_tier(tier_id)
        self.setObjectName("tierSegment")
        self.setCheckable(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        # QToolButton setText 不支持 HTML，两行文案靠文本中的 \n 换行
        price = self.t.price
        self.setText(f"{self.t.label}\n{price}")

        # 显式设置 setSizePolicy 防止被 stretch 拉宽
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        # 自适应高度(两行文字)
        font_metrics = self.fontMetrics()
        h = font_metrics.height() * 2 + 18
        self.setMinimumHeight(h)


class TopTierBar(QFrame):
    """顶栏 Tier 分段控件容器。"""

    tier_selected = Signal(str)      # 携带 tier_id
    file_upload_selected = Signal()  # 「文件转写」页面
    sidebar_toggle_requested = Signal()  # 汉堡按钮

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TopTierBar")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedHeight(56)

        layout = QHBoxLayout(self)
        layout.setSpacing(0)
        layout.setContentsMargins(
            Metrics.SPACING_SM, Metrics.SPACING_SM,
            Metrics.SPACING_LG, Metrics.SPACING_SM,
        )

        # 汉堡按钮（折叠/展开侧边栏）
        self.btn_sidebar = QPushButton()
        self.btn_sidebar.setObjectName("toolButton")
        self.btn_sidebar.setFixedSize(36, 36)
        self.btn_sidebar.setIcon(make_icon("menu", 20))
        self.btn_sidebar.setToolTip("折叠/展开侧边栏")
        self.btn_sidebar.clicked.connect(self.sidebar_toggle_requested.emit)
        layout.addWidget(self.btn_sidebar)
        layout.addSpacing(Metrics.SPACING_SM)

        # 标题 + 麦克风图标
        p = get_palette()
        title_icon = QLabel()
        title_icon.setPixmap(icon_pixmap("mic", 22, p.PRIMARY))
        title_icon.setFixedSize(24, 24)
        layout.addWidget(title_icon)
        layout.addSpacing(Metrics.SPACING_SM)

        title = QLabel("AI 录音")
        title.setObjectName("titleLabel")
        layout.addWidget(title)
        layout.addSpacing(Metrics.SPACING_LG)

        # 4 档 tier 按钮
        self._buttons: dict[str, _TierSegmentButton] = {}
        tiers = list_tiers()
        for i, t in enumerate(tiers):
            btn = _TierSegmentButton(t.tier_id)
            self._buttons[t.tier_id] = btn
            btn.toggled.connect(
                lambda checked, tid=t.tier_id: self._on_btn_toggled(tid, checked)
            )
            layout.addWidget(btn)

        # 分隔空隙
        layout.addSpacing(Metrics.SPACING_MD)

        # 第 5 个按钮:「文件转写」独立页面(非 tier)
        self._upload_btn = QToolButton()
        self._upload_btn.setObjectName("tierSegment")
        self._upload_btn.setCheckable(True)
        self._upload_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._upload_btn.setText("文件转写\n上传音频")
        self._upload_btn.setToolTip("上传音频文件进行转写,支持四种转写方式")
        self._upload_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        font_metrics = self._upload_btn.fontMetrics()
        h = font_metrics.height() * 2 + 18
        self._upload_btn.setMinimumHeight(h)
        self._upload_btn.toggled.connect(self._on_upload_toggled)
        layout.addWidget(self._upload_btn)

        # 程序化切换标志:防止 set_active 触发信号
        self._programmatic = False
        layout.addStretch()
        # 右侧留空(后续可加设置图标等),保持布局稳定

    def _any_segment_checked(self) -> bool:
        """任一分段按钮（tier/上传）是否处于选中态。"""
        return (
            any(b.isChecked() for b in self._buttons.values())
            or self._upload_btn.isChecked()
        )

    def _on_btn_toggled(self, tier_id: str, checked: bool) -> None:
        """tier 按钮 toggle 槽:维护 exclusive + emit 信号。

        用户点击已选中的按钮时 Qt 会把它翻转为 unchecked 且不重发信号，
        顶栏会陷入"无任何选中"状态。此处检测到该情形后把自身勾回，
        保持高亮一致（不重复发 tier_selected，避免绕过/重复触发守卫）。
        """
        if checked:
            if self._programmatic:
                return
            self._uncheck_others(except_tier=tier_id)
            self.tier_selected.emit(tier_id)
            return
        if self._programmatic:
            return  # 程序化互斥取消（_uncheck_others），属正常流程
        # 用户把唯一选中的按钮点掉 → 勾回，顶栏不允许无选中态
        if not self._any_segment_checked():
            btn = self._buttons.get(tier_id)
            if btn is not None:
                btn.setChecked(True)

    def _on_upload_toggled(self, checked: bool) -> None:
        """上传按钮 toggle 槽。"""
        if not checked:
            if self._programmatic:
                return
            # 与 tier 按钮同理：禁止把自己点掉后陷入无选中态
            if not self._any_segment_checked():
                self._upload_btn.setChecked(True)
            return
        if self._programmatic:
            return
        self._uncheck_others(except_tier=None)
        self.file_upload_selected.emit()

    def _uncheck_others(self, except_tier: str) -> None:
        """取消其他按钮选中(维护 single-exclusive)。"""
        self._programmatic = True
        try:
            for tid, btn in self._buttons.items():
                if tid != except_tier and btn.isChecked():
                    btn.setChecked(False)
            if except_tier is None:
                pass
            elif self._upload_btn.isChecked():
                self._upload_btn.setChecked(False)
        finally:
            self._programmatic = False

    def set_active(self, tier_id: str) -> None:
        """程序化选中指定 tier 项,取消上传按钮,不触发信号。"""
        if tier_id not in self._buttons:
            tier_id = TIER_ORDER[0]
        self._programmatic = True
        try:
            for tid, btn in self._buttons.items():
                btn.setChecked(tid == tier_id)
            self._upload_btn.setChecked(False)
        finally:
            self._programmatic = False

    def set_active_upload(self) -> None:
        """程序化选中「文件转写」按钮,取消所有 tier,不触发信号。"""
        self._programmatic = True
        try:
            for btn in self._buttons.values():
                btn.setChecked(False)
            self._upload_btn.setChecked(True)
        finally:
            self._programmatic = False
