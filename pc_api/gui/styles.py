"""全局样式系统：配色、QSS、动画工具。

支持浅色/深色双主题，通过 apply_global_styles(app, theme) 切换。
QSS 由 _build_qss(palette) 动态生成，两套主题共用同一模板。

遵循 PySide6 UI 开发规范（Property 为 PySide6 专属，PyQt6 不兼容）：
- 所有样式在 QApplication 级别统一应用，禁止在单个控件上 inline setStyleSheet。
- 控件通过 objectName / class 选择器区分样式。
- 动画通过 QPropertyAnimation / QVariantAnimation 实现，不阻塞主线程。
"""

from PySide6.QtCore import (
    QPropertyAnimation, QVariantAnimation, QEasingCurve, Qt, Property,
)
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget


# ============================================================
# 调色板
# ============================================================

class _PaletteBase:
    """配色抽象：子类填充具体色值，_build_qss 按属性名引用。"""

    BG_PRIMARY: str = ""
    BG_SECONDARY: str = ""
    BG_CARD: str = ""

    PRIMARY: str = ""
    PRIMARY_HOVER: str = ""
    PRIMARY_LIGHT: str = ""

    TEXT_PRIMARY: str = ""
    TEXT_SECONDARY: str = ""
    TEXT_MUTED: str = ""
    TEXT_ON_PRIMARY: str = ""

    BORDER: str = ""
    BORDER_FOCUS: str = ""
    DIVIDER: str = ""

    SUCCESS: str = "#22C55E"
    WARNING: str = "#F59E0B"
    ERROR: str = "#EF4444"
    ERROR_LIGHT: str = ""
    ERROR_ON_LIGHT: str = ""
    RECORDING: str = "#EF4444"

    ACCENT_FREE: str = ""
    ACCENT_REALTIME: str = ""
    ACCENT_PRO_LITE: str = ""
    ACCENT_PRO_PRO: str = ""

    SHADOW_LIGHT: str = ""
    SHADOW_MEDIUM: str = ""

    # 气泡背景（说话人分段展示）
    BUBBLE_SELF: str = ""
    BUBBLE_OTHER: str = ""
    BUBBLE_SELF_TEXT: str = ""
    BUBBLE_OTHER_TEXT: str = ""


class LightPalette(_PaletteBase):
    """浅色主题配色（teal 主色）。"""

    BG_PRIMARY = "#F8FAFC"
    BG_SECONDARY = "#F1F5F9"
    BG_CARD = "#FFFFFF"

    PRIMARY = "#0D9488"
    PRIMARY_HOVER = "#0F766E"
    PRIMARY_LIGHT = "#CCFBF1"

    TEXT_PRIMARY = "#1E293B"
    TEXT_SECONDARY = "#475569"
    TEXT_MUTED = "#64748B"
    TEXT_ON_PRIMARY = "#FFFFFF"

    BORDER = "#E2E8F0"
    BORDER_FOCUS = "#5EEAD4"
    DIVIDER = "#E2E8F0"

    ERROR_LIGHT = "#FCA5A5"
    ERROR_ON_LIGHT = "#FEF2F2"
    RECORDING = "#EF4444"

    ACCENT_FREE = "#64748B"
    ACCENT_REALTIME = "#0D9488"
    ACCENT_PRO_LITE = "#4F46E5"
    ACCENT_PRO_PRO = "#7C3AED"

    SHADOW_LIGHT = "rgba(15, 23, 42, 0.06)"
    SHADOW_MEDIUM = "rgba(15, 23, 42, 0.10)"

    BUBBLE_SELF = "#CCFBF1"
    BUBBLE_OTHER = "#F1F5F9"
    BUBBLE_SELF_TEXT = "#0F766E"
    BUBBLE_OTHER_TEXT = "#1E293B"


class DarkPalette(_PaletteBase):
    """深色主题配色（teal 主色，暗色基底）。"""

    BG_PRIMARY = "#0F172A"       # slate-900
    BG_SECONDARY = "#1E293B"     # slate-800
    BG_CARD = "#1E293B"          # slate-800

    PRIMARY = "#14B8A6"          # teal-500（深色模式提亮一档）
    PRIMARY_HOVER = "#2DD4BF"    # teal-400
    PRIMARY_LIGHT = "#134E4A"    # teal-900

    TEXT_PRIMARY = "#F1F5F9"     # slate-100
    TEXT_SECONDARY = "#CBD5E1"   # slate-300
    TEXT_MUTED = "#94A3B8"       # slate-400
    TEXT_ON_PRIMARY = "#FFFFFF"

    BORDER = "#334155"           # slate-700
    BORDER_FOCUS = "#2DD4BF"     # teal-400
    DIVIDER = "#334155"

    ERROR_LIGHT = "#7F1D1D"      # red-900
    ERROR_ON_LIGHT = "#FCA5A5"
    RECORDING = "#F87171"        # red-400（深色模式提亮）

    ACCENT_FREE = "#94A3B8"      # slate-400
    ACCENT_REALTIME = "#14B8A6"
    ACCENT_PRO_LITE = "#818CF8"  # indigo-400
    ACCENT_PRO_PRO = "#A78BFA"   # violet-400

    SHADOW_LIGHT = "rgba(0, 0, 0, 0.3)"
    SHADOW_MEDIUM = "rgba(0, 0, 0, 0.5)"

    BUBBLE_SELF = "#134E4A"      # teal-900
    BUBBLE_OTHER = "#334155"     # slate-700
    BUBBLE_SELF_TEXT = "#5EEAD4" # teal-300
    BUBBLE_OTHER_TEXT = "#F1F5F9"


# 当前激活的调色板（apply_global_styles 时设置）
_active_palette: _PaletteBase = LightPalette()


def get_palette() -> _PaletteBase:
    """获取当前激活的调色板。"""
    return _active_palette


# 兼容旧代码：`Palette.PRIMARY` 等类属性访问在旧代码中大量使用。
# 类定义里的 __getattr__ 只对实例生效，类属性访问会抛 AttributeError，
# 故改为模块级单例实例，实例属性查找失败时经 __getattr__ 代理到当前激活调色板。
class _PaletteProxy:
    """属性代理：转发到当前激活调色板。"""
    def __getattr__(self, name):
        return getattr(_active_palette, name)


Palette = _PaletteProxy()


# ============================================================
# 尺寸
# ============================================================
class Metrics:
    RADIUS_SMALL = 6
    RADIUS_MEDIUM = 10
    RADIUS_LARGE = 14
    SPACING_XS = 6
    SPACING_SM = 10
    SPACING_MD = 16
    SPACING_LG = 24
    SPACING_XL = 32


# 统一字体栈：Inter 跨平台一致 → 系统中文回退 → 通用 sans-serif
_FONT_STACK = '"Inter", "Microsoft YaHei UI", "PingFang SC", "Segoe UI", "Noto Sans CJK SC", sans-serif'


# ============================================================
# QSS 生成
# ============================================================

def _build_qss(p: _PaletteBase) -> str:
    """根据调色板生成完整 QSS 字符串。"""
    return f"""
/* ===== 基础 ===== */
QWidget {{
    font-family: {_FONT_STACK};
    font-size: 14px;
    color: {p.TEXT_PRIMARY};
    outline: none;
}}

QPushButton:focus-visible, QLineEdit:focus-visible, QTextEdit:focus-visible,
QComboBox:focus-visible, QAbstractSpinBox:focus-visible,
QListWidget:focus-visible, QTreeWidget:focus-visible, QTableWidget:focus-visible {{
    outline: 2px solid {p.BORDER_FOCUS};
}}

QMainWindow {{
    background: {p.BG_PRIMARY};
}}

QDialog {{
    background: {p.BG_PRIMARY};
}}

/* ===== 按钮 ===== */
QPushButton {{
    background: {p.BG_CARD};
    color: {p.TEXT_PRIMARY};
    border: 1px solid {p.BORDER};
    border-radius: {Metrics.RADIUS_MEDIUM}px;
    padding: 8px 18px;
    min-height: 34px;
}}

QPushButton:hover {{
    background: {p.BG_SECONDARY};
    border-color: {p.TEXT_MUTED};
}}

QPushButton:pressed {{
    background: {p.BORDER};
}}

QPushButton:disabled {{
    background: {p.BG_SECONDARY};
    color: {p.TEXT_MUTED};
    border-color: {p.BORDER};
}}

QPushButton#primaryButton {{
    background: {p.PRIMARY};
    color: {p.TEXT_ON_PRIMARY};
    border: none;
    font-weight: 600;
}}

QPushButton#primaryButton:hover {{
    background: {p.PRIMARY_HOVER};
}}

QPushButton#primaryButton:disabled {{
    background: {p.PRIMARY_LIGHT};
    color: {p.TEXT_MUTED};
}}

QPushButton#dangerButton {{
    background: {p.ERROR};
    color: {p.TEXT_ON_PRIMARY};
    border: none;
    font-weight: 600;
}}

QPushButton#dangerButton:hover {{
    background: #DC2626;
}}

QPushButton#dangerButton:disabled {{
    background: {p.ERROR_LIGHT};
    color: {p.ERROR_ON_LIGHT};
}}

QPushButton#toolButton {{
    background: transparent;
    border: none;
    padding: 6px 10px;
    border-radius: {Metrics.RADIUS_SMALL}px;
}}

QPushButton#toolButton:hover {{
    background: {p.BG_SECONDARY};
}}

QPushButton#ghostButton {{
    background: transparent;
    color: {p.TEXT_SECONDARY};
    border: 1px solid {p.BORDER};
    border-radius: {Metrics.RADIUS_MEDIUM}px;
    padding: 8px 18px;
    min-height: 34px;
}}
QPushButton#ghostButton:hover {{
    background: {p.BG_SECONDARY};
    color: {p.TEXT_PRIMARY};
    border-color: {p.TEXT_MUTED};
}}

/* ===== 输入框 ===== */
QLineEdit, QTextEdit, QComboBox {{
    background: {p.BG_CARD};
    border: 1px solid {p.BORDER};
    border-radius: {Metrics.RADIUS_SMALL}px;
    padding: 8px 12px;
    color: {p.TEXT_PRIMARY};
}}

QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{
    border: 1.5px solid {p.PRIMARY};
}}

QLineEdit:disabled, QTextEdit:disabled, QComboBox:disabled {{
    background: {p.BG_SECONDARY};
    color: {p.TEXT_MUTED};
}}

QTextEdit {{
    padding: 10px 12px;
}}

QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
    border-left: 1px solid {p.BORDER};
}}

QComboBox QAbstractItemView {{
    background: {p.BG_CARD};
    border: 1px solid {p.BORDER};
    border-radius: {Metrics.RADIUS_SMALL}px;
    selection-background-color: {p.PRIMARY_LIGHT};
    selection-color: {p.TEXT_PRIMARY};
    padding: 4px;
}}

/* ===== 标签 ===== */
QLabel {{
    color: {p.TEXT_PRIMARY};
}}

QLabel#titleLabel {{
    font-size: 18px;
    font-weight: 700;
    color: {p.TEXT_PRIMARY};
}}

QLabel#subtitleLabel {{
    font-size: 13px;
    color: {p.TEXT_SECONDARY};
}}

QLabel#cardTitle {{
    font-size: 15px;
    font-weight: 600;
    color: {p.TEXT_PRIMARY};
}}

QLabel#statusLabel {{
    font-size: 13px;
    color: {p.TEXT_SECONDARY};
    padding: 4px 8px;
    border-radius: {Metrics.RADIUS_SMALL}px;
}}

QLabel#statusLabel[recording="true"] {{
    color: {p.ERROR};
    background: rgba(239, 68, 68, 0.08);
    font-weight: 600;
}}

/* ===== 分组框 ===== */
QGroupBox {{
    background: {p.BG_CARD};
    border: 1px solid {p.BORDER};
    border-radius: {Metrics.RADIUS_MEDIUM}px;
    margin-top: 14px;
    padding-top: 18px;
    padding-bottom: 12px;
    padding-left: 16px;
    padding-right: 16px;
    font-weight: 600;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    top: -12px;
    padding: 2px 8px;
    background: {p.BG_CARD};
    color: {p.TEXT_PRIMARY};
    border-radius: {Metrics.RADIUS_SMALL}px;
    border: 1px solid {p.BORDER};
}}

/* ===== 滚动区域 / 滚动条 ===== */
QScrollArea {{
    border: none;
    background: transparent;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical {{
    background: {p.TEXT_MUTED};
    min-height: 40px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical:hover {{
    background: {p.TEXT_SECONDARY};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
    border-radius: 4px;
}}

QScrollBar::handle:horizontal {{
    background: {p.TEXT_MUTED};
    min-width: 40px;
    border-radius: 4px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {p.TEXT_SECONDARY};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

/* ===== 分割条 ===== */
QSplitter::handle {{
    background: {p.DIVIDER};
}}

QSplitter::handle:horizontal {{
    width: 2px;
}}

QSplitter::handle:vertical {{
    height: 2px;
}}

QSplitter::handle:hover {{
    background: {p.PRIMARY};
}}

/* ===== 状态栏 ===== */
QStatusBar {{
    background: {p.BG_CARD};
    color: {p.TEXT_SECONDARY};
    border-top: 1px solid {p.BORDER};
    padding: 2px 12px;
}}

QStatusBar::item {{
    border: none;
}}

/* ===== 对话框按钮框 ===== */
QDialogButtonBox QPushButton {{
    min-width: 82px;
}}

/* ===== 卡片 ===== */
QFrame#Card {{
    background: {p.BG_CARD};
    border: 1px solid {p.BORDER};
    border-radius: {Metrics.RADIUS_MEDIUM}px;
}}

QFrame#AttachmentThumbnail {{
    background: {p.BG_CARD};
    border: 1px solid {p.BORDER};
    border-radius: {Metrics.RADIUS_MEDIUM}px;
}}

QFrame#AttachmentThumbnail:hover {{
    border-color: {p.PRIMARY};
}}

/* ===== 版本头部卡片 ===== */
QFrame#TierHeader {{
    background: {p.BG_CARD};
    border: 1px solid {p.BORDER};
    border-radius: {Metrics.RADIUS_MEDIUM}px;
}}

QFrame#TierAccentBar {{
    border: none;
    border-top-left-radius: {Metrics.RADIUS_MEDIUM}px;
    border-top-right-radius: {Metrics.RADIUS_MEDIUM}px;
}}

QFrame#TierAccentBar[accent="free"] {{ background: {p.ACCENT_FREE}; }}
QFrame#TierAccentBar[accent="realtime"] {{ background: {p.ACCENT_REALTIME}; }}
QFrame#TierAccentBar[accent="pro_lite"] {{ background: {p.ACCENT_PRO_LITE}; }}
QFrame#TierAccentBar[accent="pro_pro"] {{ background: {p.ACCENT_PRO_PRO}; }}

QLabel#tierTitle {{
    font-size: 22px; font-weight: 700; color: {p.TEXT_PRIMARY};
}}

QLabel#tierSubtitle {{
    font-size: 13px; color: {p.TEXT_SECONDARY};
}}

QLabel#tierPriceBadge {{
    background: {p.PRIMARY_LIGHT};
    color: {p.PRIMARY};
    border-radius: {Metrics.RADIUS_SMALL}px;
    padding: 6px 14px; font-weight: 600; font-size: 13px; min-width: 60px;
}}

QLabel#tierFeature {{
    background: {p.BG_SECONDARY};
    color: {p.TEXT_SECONDARY};
    border-radius: {Metrics.RADIUS_SMALL}px;
    padding: 6px 10px; font-size: 12px;
}}

/* ===== 选项卡 ===== */
QTabWidget::pane {{
    background: {p.BG_CARD};
    border: 1px solid {p.BORDER};
    border-top: none;
    border-radius: 0 0 {Metrics.RADIUS_MEDIUM}px {Metrics.RADIUS_MEDIUM}px;
    top: -1px;
}}

QTabBar {{
    background: transparent;
    qproperty-drawBase: 0;
}}

QTabBar::tab {{
    background: transparent;
    color: {p.TEXT_SECONDARY};
    padding: 10px 22px;
    min-width: 96px;
    border: 1px solid transparent;
    border-bottom: 2px solid {p.BORDER};
    font-weight: 500;
    font-size: 14px;
}}

QTabBar::tab:hover {{
    color: {p.PRIMARY};
    background: {p.BG_SECONDARY};
}}

QTabBar::tab:selected {{
    color: {p.PRIMARY};
    border-bottom: 2px solid {p.PRIMARY};
    font-weight: 600;
}}

QTabBar::tab:!selected {{
    margin-top: 0px;
}}

/* ===== 菜单 ===== */
QMenu {{
    background: {p.BG_CARD};
    border: 1px solid {p.BORDER};
    padding: 6px;
}}
QMenu::item {{
    padding: 6px 18px;
    border-radius: {Metrics.RADIUS_SMALL}px;
}}
QMenu::item:selected {{
    background: {p.PRIMARY_LIGHT};
    color: {p.TEXT_PRIMARY};
}}

/* ===== 顶栏分段控件 ===== */
QFrame#TopTierBar {{
    background: {p.BG_CARD};
    border-bottom: 1px solid {p.BORDER};
}}

QToolButton#tierSegment {{
    background: {p.BG_CARD};
    color: {p.TEXT_SECONDARY};
    border: 1px solid {p.BORDER};
    border-right: none;
    padding: 6px 18px;
    font-weight: 500;
    min-height: 30px;
}}
QToolButton#tierSegment:first {{
    border-top-left-radius: {Metrics.RADIUS_SMALL}px;
    border-bottom-left-radius: {Metrics.RADIUS_SMALL}px;
}}
QToolButton#tierSegment:last {{
    border-right: 1px solid {p.BORDER};
    border-top-right-radius: {Metrics.RADIUS_SMALL}px;
    border-bottom-right-radius: {Metrics.RADIUS_SMALL}px;
}}
QToolButton#tierSegment:hover:!checked {{
    background: {p.BG_SECONDARY};
    color: {p.TEXT_PRIMARY};
}}
QToolButton#tierSegment:checked {{
    background: {p.PRIMARY};
    color: {p.TEXT_ON_PRIMARY};
    border: 1px solid {p.PRIMARY};
    border-right: none;
    font-weight: 600;
}}
QToolButton#tierSegment:last:checked {{
    border-right: 1px solid {p.PRIMARY};
}}
QToolButton#tierSegment:checked QLabel#tierSegmentPrice {{
    color: rgba(255, 255, 255, 0.85);
}}
QLabel#tierSegmentPrice {{
    color: {p.TEXT_MUTED};
    font-size: 11px;
}}

/* ===== 历史侧边栏 ===== */
QFrame#SessionHistorySidebar {{
    background: {p.BG_SECONDARY};
    border-right: 1px solid {p.BORDER};
}}
QLineEdit#sessionSearch {{
    background: {p.BG_CARD};
    border: 1px solid {p.BORDER};
    border-radius: {Metrics.RADIUS_SMALL}px;
    padding: 6px 10px;
    color: {p.TEXT_PRIMARY};
}}
QLineEdit#sessionSearch:focus {{
    border: 1.5px solid {p.PRIMARY};
}}
QListWidget#SessionHistoryList {{
    background: transparent;
    border: none;
    outline: none;
    padding: 4px;
}}
QListWidget#SessionHistoryList::item {{
    border-radius: {Metrics.RADIUS_SMALL}px;
    padding: 2px;
    margin: 1px 0;
}}
QListWidget#SessionHistoryList::item:hover {{
    background: {p.BG_CARD};
}}
QListWidget#SessionHistoryList::item:selected {{
    background: {p.PRIMARY_LIGHT};
    border-left: 3px solid {p.PRIMARY};
}}
QListWidget#SessionHistoryList::item:selected QLabel#sessionItemTitle {{
    color: {p.PRIMARY_HOVER};
    font-weight: 600;
}}
QLabel#sessionSectionHeader {{
    color: {p.TEXT_MUTED};
    font-size: 11px; font-weight: 600;
    padding: 12px 14px 4px 14px;
    background: transparent;
}}
QLabel#sessionItemTitle {{
    font-size: 13px; font-weight: 500; color: {p.TEXT_PRIMARY};
}}
QLabel#sessionItemMeta {{
    font-size: 11px; color: {p.TEXT_MUTED};
}}
QLabel#sessionItemStatus[status="COMPLETED"] {{ color: {p.SUCCESS}; font-size: 11px; }}
QLabel#sessionItemStatus[status="PROCESSING"] {{ color: {p.WARNING}; font-size: 11px; }}
QLabel#sessionItemStatus[status="FAILED"] {{ color: {p.ERROR}; font-size: 11px; }}
QLabel#sessionItemStatus[status="PENDING"] {{ color: {p.TEXT_MUTED}; font-size: 11px; }}
QLabel#sessionItemRecording {{
    color: {p.RECORDING}; font-size: 11px; font-weight: 700;
}}
QLabel#sessionEmptyHint {{
    color: {p.TEXT_MUTED}; font-size: 12px; padding: 24px 16px;
}}

/* ===== 历史详情视图 ===== */
QFrame#SessionDetailView {{
    background: {p.BG_PRIMARY};
}}
QFrame#detailHeader {{
    background: {p.BG_CARD};
    border: 1px solid {p.BORDER};
    border-radius: {Metrics.RADIUS_MEDIUM}px;
    padding: 4px;
}}
QLabel#detailTitle {{
    font-size: 22px; font-weight: 700; color: {p.TEXT_PRIMARY}; background: transparent;
}}
QLabel#detailMeta {{
    font-size: 12px; color: {p.TEXT_MUTED}; background: transparent;
}}
QLabel#detailStatus[status="COMPLETED"] {{ color: {p.SUCCESS}; font-size: 12px; font-weight: 600; }}
QLabel#detailStatus[status="PROCESSING"] {{ color: {p.WARNING}; font-size: 12px; font-weight: 600; }}
QLabel#detailStatus[status="FAILED"] {{ color: {p.ERROR}; font-size: 12px; font-weight: 600; }}
QLabel#detailStatus[status="PENDING"] {{ color: {p.TEXT_MUTED}; font-size: 12px; font-weight: 600; }}

/* ===== 转写气泡（说话人分段） ===== */
QFrame#BubbleSelf {{
    background: {p.BUBBLE_SELF};
    border: 2px solid transparent;
    border-radius: 12px;
}}
QFrame#BubbleOther {{
    background: {p.BUBBLE_OTHER};
    border: 2px solid transparent;
    border-radius: 12px;
}}
QFrame#BubbleSelf[highlighted="true"] {{
    border: 2px solid {p.PRIMARY};
}}
QFrame#BubbleOther[highlighted="true"] {{
    border: 2px solid {p.PRIMARY};
}}
QLabel#bubbleSpeaker {{
    color: {p.PRIMARY}; font-size: 12px; font-weight: 600;
}}
QLabel#bubbleTime {{
    color: {p.TEXT_MUTED}; font-size: 11px;
}}
QLabel#bubbleText {{
    color: {p.TEXT_PRIMARY}; font-size: 14px;
}}
QLabel#bubblePartial {{
    color: {p.TEXT_MUTED}; font-style: italic; font-size: 13px; padding: 2px 8px;
}}

/* ===== Toast 通知 ===== */
QFrame#Toast {{
    background: {p.BG_CARD};
    border: 1px solid {p.BORDER};
    border-radius: {Metrics.RADIUS_MEDIUM}px;
}}
QLabel#toastText {{
    color: {p.TEXT_PRIMARY};
    font-size: 13px;
}}

/* ===== 音频播放器 ===== */
QFrame#AudioPlayer {{
    background: {p.BG_CARD};
    border: 1px solid {p.BORDER};
    border-radius: {Metrics.RADIUS_MEDIUM}px;
}}
QSlider#audioSeek::groove:horizontal {{
    background: {p.BG_SECONDARY};
    height: 6px;
    border-radius: 3px;
}}
QSlider#audioSeek::sub-page:horizontal {{
    background: {p.PRIMARY};
    border-radius: 3px;
}}
QSlider#audioSeek::handle:horizontal {{
    background: {p.PRIMARY};
    width: 14px;
    height: 14px;
    margin: -4px 0;
    border-radius: 7px;
}}
QSlider#audioSeek::handle:horizontal:hover {{
    background: {p.PRIMARY_HOVER};
}}

/* ===== 引导页 ===== */
QFrame#OnboardingCard {{
    background: {p.BG_CARD};
    border: 1.5px solid {p.BORDER};
    border-radius: {Metrics.RADIUS_LARGE}px;
}}
QFrame#OnboardingCard:hover {{
    border: 1.5px solid {p.PRIMARY};
}}
QFrame#OnboardingCard[selected="true"] {{
    background: {p.BG_CARD};
    border: 2.5px solid {p.PRIMARY};
    border-radius: {Metrics.RADIUS_LARGE}px;
}}

QLabel#progressDot {{
    background: {p.BORDER};
    border-radius: 4px;
    min-width: 24px;
    max-width: 24px;
    min-height: 6px;
    max-height: 6px;
}}
QLabel#progressDot[active="true"] {{
    background: {p.PRIMARY};
    min-width: 32px;
    max-width: 32px;
}}
QLabel#progressDot[done="true"] {{
    background: {p.SUCCESS};
}}

QLabel#onboardingTitle {{
    font-size: 26px;
    font-weight: 700;
    color: {p.TEXT_PRIMARY};
    letter-spacing: -0.5px;
}}
QLabel#onboardingSubtitle {{
    font-size: 15px;
    color: {p.TEXT_SECONDARY};
}}
QLabel#onboardingDesc {{
    font-size: 13px;
    color: {p.TEXT_MUTED};
    line-height: 1.6;
}}
QLabel#onboardingStep {{
    font-size: 12px;
    font-weight: 600;
    color: {p.PRIMARY};
    letter-spacing: 1px;
}}

QFrame#tierAccentTop {{
    border: none;
    border-top-left-radius: {Metrics.RADIUS_LARGE}px;
    border-top-right-radius: {Metrics.RADIUS_LARGE}px;
    min-height: 4px;
    max-height: 4px;
}}
QFrame#tierAccentTop[accent="free"] {{ background: {p.ACCENT_FREE}; }}
QFrame#tierAccentTop[accent="realtime"] {{ background: {p.ACCENT_REALTIME}; }}
QFrame#tierAccentTop[accent="pro_lite"] {{ background: {p.ACCENT_PRO_LITE}; }}
QFrame#tierAccentTop[accent="pro_pro"] {{ background: {p.ACCENT_PRO_PRO}; }}

QLabel#tierIconWrap {{
    background: {p.PRIMARY_LIGHT};
    border-radius: 12px;
    padding: 12px;
}}
QLabel#tierPriceTag {{
    background: {p.BG_SECONDARY};
    color: {p.TEXT_SECONDARY};
    border-radius: {Metrics.RADIUS_SMALL}px;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 600;
}}
QLabel#tierFeatureTag {{
    color: {p.TEXT_MUTED};
    font-size: 12px;
    padding: 2px 0;
}}

QLabel#heroCircle {{
    background: {p.PRIMARY_LIGHT};
    border-radius: 36px;
}}
QLabel#heroCircleSmall {{
    background: {p.PRIMARY_LIGHT};
    border-radius: 24px;
}}
QLabel#checkCircle {{
    background: {p.PRIMARY_LIGHT};
    border-radius: 36px;
}}
"""


# 全局 QSS（兼容旧引用，实际值在 apply_global_styles 时确定）
GLOBAL_QSS = _build_qss(LightPalette())


def apply_global_styles(app: QApplication, theme: str = "light") -> None:
    """在 QApplication 上应用全局 QSS 与调色板。

    Args:
        app: QApplication 实例
        theme: "light" 或 "dark"
    """
    global _active_palette, GLOBAL_QSS

    if theme == "dark":
        _active_palette = DarkPalette()
    else:
        _active_palette = LightPalette()

    GLOBAL_QSS = _build_qss(_active_palette)
    app.setStyleSheet(GLOBAL_QSS)

    pal = app.palette()
    p = _active_palette
    pal.setColor(QPalette.ColorRole.Window, QColor(p.BG_PRIMARY))
    pal.setColor(QPalette.ColorRole.Base, QColor(p.BG_CARD))
    pal.setColor(QPalette.ColorRole.Text, QColor(p.TEXT_PRIMARY))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(p.PRIMARY))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(p.TEXT_ON_PRIMARY))
    app.setPalette(pal)


def reapply_theme(app: QApplication, theme: str) -> None:
    """切换主题（运行时调用，重新生成 QSS 并刷新所有控件）。"""
    apply_global_styles(app, theme)
    # 强制刷新所有顶层控件的样式
    for widget in app.topLevelWidgets():
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()


# ============================================================
# 动画工具
# ============================================================

class RecordingIndicator(QWidget):
    """录音状态指示灯：红色脉冲动画，不阻塞主线程。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._alpha = 255
        self.setFixedSize(14, 14)
        self._animation = QPropertyAnimation(self, b"indicatorAlpha")
        self._animation.setDuration(1400)
        self._animation.setKeyValueAt(0.0, 255)
        self._animation.setKeyValueAt(0.5, 70)
        self._animation.setKeyValueAt(1.0, 255)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._animation.setLoopCount(-1)
        self._animation.start()

    def get_indicator_alpha(self) -> int:
        return self._alpha

    def set_indicator_alpha(self, value: int):
        self._alpha = max(0, min(255, value))
        self.update()

    indicatorAlpha = Property(int, get_indicator_alpha, set_indicator_alpha)

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QBrush
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(get_palette().RECORDING)
        color.setAlpha(self._alpha)
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(self.rect())


class Spinner(QWidget):
    """简单旋转等待指示器。"""

    def __init__(self, parent=None, size: int = 24):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self._angle = 0
        self._animation = QPropertyAnimation(self, b"rotationAngle")
        self._animation.setDuration(1000)
        self._animation.setStartValue(0)
        self._animation.setEndValue(360)
        self._animation.setLoopCount(-1)
        self._animation.start()

    def get_angle(self) -> int:
        return self._angle

    def set_angle(self, value: int):
        self._angle = value % 360
        self.update()

    rotationAngle = Property(int, get_angle, set_angle)

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QPen
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(get_palette().PRIMARY))
        pen.setWidth(max(2, self._size // 10))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        rect = self.rect().adjusted(pen.width(), pen.width(), -pen.width(), -pen.width())
        painter.drawArc(rect, self._angle * 16, 270 * 16)


def fade_in(widget: QWidget, duration: int = 200) -> None:
    """对 widget 执行淡入动画（opacity 0→1）。"""
    # QGraphicsOpacityEffect 方式
    from PySide6.QtWidgets import QGraphicsOpacityEffect
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.start()
    # 防止动画对象被 GC
    widget._fade_anim = anim
    widget._fade_effect = effect
