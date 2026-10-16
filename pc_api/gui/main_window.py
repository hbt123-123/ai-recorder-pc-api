"""主窗口：顶栏 tier 分段 + 左侧会话历史 + 右侧双视图(实时工作区 / 历史详情)。

布局：
  CentralWidget (QWidget)
  ├─ TopTierBar        (顶栏,固定 56px,4 档 tier 分段控件)
  └─ ContentSplitter   (QSplitter Horizontal)
     ├─ SessionHistorySidebar (左侧会话历史,可隐藏/显示)
     └─ MainStack (QStackedWidget)
        ├─ 0..3: 4 个 BaseTierPage(实时录音工作区,懒加载)
        └─ 4   : SessionDetailView(历史详情只读)

视图切换:
  - 顶栏 tier 选中       → 切换到对应 tier 页面(实时工作区)
  - 侧边栏选中历史       → 切到 SessionDetailView + 加载该 rid
  - 「开启新对话」按钮   → 切回当前 tier 的实时工作区
  - 侧边栏删除当前 rid   → 自动切回实时工作区(由 sidebar 内部触发)

自动标题:
  BaseTierPage.transcription_saved(int, str) → 启动 AutoTitleWorker
  完成/失败后通过信号回调,刷新侧边栏条目标题。

底部「设置」弹 SettingsDialog,改完 tier 后自动跳到对应实时工作区页。

视频会议模式(保持兼容):
  主窗口可收起为 FloatingWindow(无边框置顶小窗),录音不中断。
  悬浮窗只发信号不直接调服务,MainWindow 收到后转发到当前 tier 页。
  全局热键(默认 Ctrl+Alt+S)触发当前页截屏;pynput 不可用时回退 QShortcut。
"""

import logging
import time
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt, QTimer, Signal, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QKeySequence, QShortcut, QIcon
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QSplitter, QStackedWidget,
    QVBoxLayout, QLabel, QPushButton, QStatusBar, QMessageBox,
    QApplication,
)

from pc_api.gui.styles import reapply_theme, fade_in
from pc_api.gui.icons import make_icon

from core.fusion import NoteFuser
from core.transcription.tiers import TIER_ORDER, get_tier
from core.user_config import UserConfig, save_user_config
from pc_api.config_loader import apply_config
from pc_api.recording_service import RecordingService
from pc_api.gui.top_tier_bar import TopTierBar
from pc_api.gui.session_history_sidebar import SessionHistorySidebar
from pc_api.gui.session_detail_view import SessionDetailView
from pc_api.gui.auto_title_worker import AutoTitleWorker
from pc_api.gui.pages import make_tier_page
from pc_api.gui.pages.file_upload_page import FileUploadPage
from pc_api.gui.settings_dialog import SettingsDialog
from pc_api.gui.floating_window import FloatingWindow
from pc_api.global_hotkey import (
    GlobalHotkeyManager, normalize_hotkey, validate_hotkey,
)

if TYPE_CHECKING:  # 仅类型标注使用，避免运行时循环导入
    from pc_api.gui.pages.base_page import BaseTierPage

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """应用主窗口：顶栏 tier + 左侧历史 + 右侧双视图。"""

    # 全局热键触发信号：pynput 在监听线程触发,需 QueuedConnection 切回主线程
    _hotkey_triggered = Signal()

    def __init__(self, service: RecordingService, cfg: UserConfig):
        super().__init__()
        self.service = service
        self.cfg = cfg
        self.setWindowTitle("AI 录音（PC API 版）")
        self.resize(1280, 820)

        # 窗口图标
        from pathlib import Path as _P
        _icon = _P(__file__).resolve().parent.parent.parent / "assets" / "app.ico"
        if _icon.exists():
            self.setWindowIcon(QIcon(str(_icon)))

        # 懒加载:tier 页面首次切换到时才构造,避免启动即实例化全部 4 页
        self._page_instances: dict[str, "BaseTierPage"] = {}
        # 当前激活的 tier_id(实时工作区),用于「开启新对话」切回
        self._current_tier: str = cfg.asr_tier
        # 当前 MainStack 显示模式:True=历史详情,False=实时工作区
        self._in_detail_mode: bool = False
        # 录音中的 rid(用于侧边栏红点)
        self._active_recording_rid: Optional[int] = None
        # AutoTitleWorker 句柄(用于避免并发 + closeEvent 清理)
        self._title_worker: Optional[AutoTitleWorker] = None
        # 共享的 NoteFuser(用于 AutoTitleWorker,延后到首调用时构造以兼容无 API key)
        self._note_fuser: Optional[NoteFuser] = None
        # 侧边栏折叠状态(折叠动画用)
        self._sidebar_collapsed: bool = False
        self._sidebar_width_saved: int = 260
        self._sidebar_anim = None

        self._build_ui()
        self._build_floating_window()
        self._connect()

        # 全局热键管理器(pynput 不可用时回退 QShortcut)
        self._hotkey_mgr = GlobalHotkeyManager()
        self._fallback_shortcut = None  # QShortcut 回退
        self._registered_hotkey: Optional[str] = None  # 当前已注册的组合键
        # 常驻隐藏 widget 作为应用内 QShortcut 的 owner。
        # 不能绑定到 self(MainWindow):主窗口 hide(悬浮窗模式)后 QShortcut 失效。
        # ApplicationShortcut 上下文下,只要应用任一窗口活动即生效。
        self._hotkey_owner = QWidget()
        self._hotkey_owner.hide()
        # 热键触发 → 截屏(QueuedConnection 跨线程安全)
        self._hotkey_triggered.connect(self._on_hotkey_screenshot,
                                       Qt.ConnectionType.QueuedConnection)

        # 默认激活 cfg.asr_tier(不落盘)
        self._switch_to_tier(cfg.asr_tier, persist=False)
        # 注册全局热键(在 _switch_to_tier 后,确保 _current_page 可用)
        self._register_hotkey()

        # 启动后异步刷新一次侧边栏(避免启动时同步刷新阻塞 UI)
        QTimer.singleShot(0, self.sidebar.refresh)

    def _build_ui(self):
        """构造顶栏 + 主体 splitter。"""
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setSpacing(0)
        central_layout.setContentsMargins(0, 0, 0, 0)

        # 1. 顶栏 tier 分段控件
        self.top_bar = TopTierBar()
        central_layout.addWidget(self.top_bar)

        # 2. 主体 splitter
        self.sidebar = SessionHistorySidebar(self.service)
        # 允许折叠到 0 宽(默认 QSplitter 尊重最小宽度,折叠动画会卡住)
        self.sidebar.setMinimumWidth(0)
        self.stack = QStackedWidget()

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(2)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self.stack)
        # 初始宽度:侧边栏 260,主区域 1020(可拖动调整)
        self.splitter.setSizes([260, 1020])
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        central_layout.addWidget(self.splitter, 1)

        self.setCentralWidget(central)

        # 3. 文件转写页（常驻 stack,位于 detail 之前）
        self.upload_page = FileUploadPage(self.service, self.cfg)
        self.stack.addWidget(self.upload_page)

        # 4. 详情视图（常驻在 stack 末尾）
        self.detail_view = SessionDetailView(self.service)
        self.stack.addWidget(self.detail_view)

        # 4. 底部状态栏
        self.setStatusBar(QStatusBar())
        self.lbl_current_tier = QLabel("")
        self.lbl_current_tier.setObjectName("subtitleLabel")
        self.statusBar().addWidget(self.lbl_current_tier)

        self.btn_settings = QPushButton(" 设置")
        self.btn_settings.setObjectName("toolButton")
        self.btn_settings.setIcon(make_icon("settings", 16))
        self.statusBar().addPermanentWidget(self.btn_settings)

        # 主题切换按钮（深色/浅色）
        self.btn_theme = QPushButton()
        self.btn_theme.setObjectName("toolButton")
        self.btn_theme.setFixedSize(32, 32)
        self._update_theme_button()
        self.statusBar().addPermanentWidget(self.btn_theme)

    def _build_floating_window(self):
        """构造悬浮窗(初始隐藏)+ 驱动定时器。

        悬浮窗的信号转发到「当前 tier 页」的 RecordingWidget:
          - recording_started / recording_stopped → rw.on_start / rw.on_stop
          - expand_requested → self._toggle_floating(展开主窗口)
        """
        self.floating = FloatingWindow()
        self.floating.setVisible(False)

        # 200ms 定时器:同步悬浮窗计时 + 音量(与 RecordingWidget._timer 同节奏)
        self._floating_timer = QTimer(self)
        self._floating_timer.setInterval(200)
        self._floating_timer.timeout.connect(self._update_floating_metrics)

        # 连接悬浮窗信号(槽函数在 _current_page 取实时页,避免持有过期引用)
        self.floating.recording_started.connect(self._on_floating_start)
        self.floating.recording_stopped.connect(self._on_floating_stop)
        self.floating.pause_toggled.connect(self._on_floating_pause_toggle)
        self.floating.expand_requested.connect(self._toggle_floating)

    def _connect(self):
        # 顶栏 tier 切换 + 文件转写页 + 侧边栏折叠
        self.top_bar.tier_selected.connect(self._switch_to_tier)
        self.top_bar.file_upload_selected.connect(self._switch_to_upload_page)
        self.top_bar.sidebar_toggle_requested.connect(self._toggle_sidebar)
        # 侧边栏
        self.sidebar.session_selected.connect(self._on_session_selected)
        self.sidebar.recording_updated.connect(self._on_sidebar_recording_updated)
        self.sidebar.new_session_requested.connect(self._switch_to_workspace)
        # 详情视图
        self.detail_view.session_renamed.connect(self._on_session_renamed)
        self.detail_view.session_deleted.connect(self._on_session_deleted)
        self.detail_view.retrain_requested.connect(self._on_retrain_requested)
        self.detail_view.regen_note_requested.connect(self._on_regen_note_requested)
        # 文件转写页桥接信号(复用 AutoTitleWorker + 侧边栏刷新)
        self.upload_page.transcription_saved.connect(self._on_transcription_saved)
        self.upload_page.recording_session.connect(self._on_recording_session)
        # 设置按钮 + 主题切换
        self.btn_settings.clicked.connect(self._open_settings)
        self.btn_theme.clicked.connect(self._toggle_theme)

    # ---- 当前页访问 ----

    def _current_page(self):
        """返回当前显示的录音工作区 BaseTierPage（仅实时工作区模式）。

        文件转写页 / 历史详情视图没有 recording_widget，统一返回 None，
        使悬浮窗、全局热键等依赖录音控件的逻辑自动失效。
        """
        if self._in_detail_mode:
            return None
        w = self.stack.currentWidget()
        if w is None or w is self.upload_page or w is self.detail_view:
            return None
        return w

    def _connect_page_signals(self, page) -> None:
        """连接页面的桥接信号到 MainWindow(每次页面首次构造时调用一次)。

        - float_requested: 用户点录音控件的「悬浮窗」→ 收起主窗口
        - rec_state_changed: 录音开始/停止 → 同步悬浮窗按钮态 + 计时器启停
        - meeting_config_changed: 会议模式开关/音源变更 → 广播到其他已构造页面同步
        - transcription_saved: 转写已落库 → 启动 AutoTitleWorker
        - recording_session: 录音会话变化(rid+bool)→ 侧边栏红点 + 新条目入库
        """
        try:
            page.float_requested.connect(self._toggle_floating)
        except Exception as e:
            logger.warning(f"连接 page.float_requested 失败: {e}")
        try:
            page.rec_state_changed.connect(self._on_rec_state_changed)
        except Exception as e:
            logger.warning(f"连接 page.rec_state_changed 失败: {e}")
        try:
            page.rec_paused_changed.connect(self._on_rec_paused_changed)
        except Exception as e:
            logger.warning(f"连接 page.rec_paused_changed 失败: {e}")
        try:
            page.meeting_config_changed.connect(self._broadcast_meeting_config)
        except Exception as e:
            logger.warning(f"连接 page.meeting_config_changed 失败: {e}")
        try:
            page.transcription_saved.connect(self._on_transcription_saved)
        except Exception as e:
            logger.warning(f"连接 page.transcription_saved 失败: {e}")
        try:
            page.recording_session.connect(self._on_recording_session)
        except Exception as e:
            logger.warning(f"连接 page.recording_session 失败: {e}")

    def _broadcast_meeting_config(self) -> None:
        """某页会议模式/音源变更 → 广播到其他已构造页面同步 UI。

        发起者页面已自行更新 cfg 与本地 UI,无需重复 apply;其他页面持有的
        self.cfg 是同一引用(同步更新),但 UI 控件态需显式刷新。
        apply_meeting_config 内部 blockSignals 防止回环。
        """
        sender = self.sender()
        for page in self._page_instances.values():
            if page is sender:
                continue
            try:
                page.apply_meeting_config()
            except Exception as e:
                logger.warning(f"广播会议配置失败: {e}")

    # ---- 视图切换 ----

    def _toggle_sidebar(self) -> None:
        """折叠/展开侧边栏(200ms 宽度动画)。"""
        sizes = self.splitter.sizes()
        if not sizes or len(sizes) < 2:
            return
        if self._sidebar_collapsed:
            # 展开:恢复到折叠前宽度
            target_w = self._sidebar_width_saved
            self._sidebar_collapsed = False
        else:
            # 折叠:记录当前宽度 → 动画到 0
            self._sidebar_width_saved = sizes[0]
            target_w = 0
            self._sidebar_collapsed = True
        total = sizes[0] + sizes[1]
        target_w = max(0, min(target_w, total))
        self._animate_sidebar_width(sizes[0], target_w, total)

    def _animate_sidebar_width(self, start: int, end: int, total: int) -> None:
        """用 QVariantAnimation 平滑改变 splitter 两侧宽度。"""
        # 停掉并销毁旧动画：快速连续切换时旧动画仍在运行，两个
        # valueChanged 会同时写 setSizes 产生宽度抖动/回拉
        old = self._sidebar_anim
        if old is not None:
            try:
                old.stop()
                old.deleteLater()
            except Exception:
                pass
        anim = QVariantAnimation(self)
        anim.setDuration(200)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(float(start))
        anim.setEndValue(float(end))
        anim.valueChanged.connect(
            lambda w: self.splitter.setSizes([int(w), total - int(w)])
        )
        anim.start()
        self._sidebar_anim = anim  # 防止 GC

    def _toggle_theme(self) -> None:
        """切换深色/浅色主题(写回配置 + 全局重刷 QSS)。"""
        new_theme = "dark" if self.cfg.theme != "dark" else "light"
        self.cfg.theme = new_theme
        try:
            save_user_config(self.cfg)
        except Exception as e:
            logger.warning(f"保存主题配置失败: {e}")
        app = QApplication.instance()
        if app is not None:
            reapply_theme(app, new_theme)
        self._update_theme_button()
        self.statusBar().showMessage(
            "已切换到深色模式" if new_theme == "dark" else "已切换到浅色模式", 2000
        )

    def _update_theme_button(self) -> None:
        """按当前主题刷新主题切换按钮的图标/提示。"""
        dark = self.cfg.theme == "dark"
        # 图标表示「将要切换到的模式」:浅色时显示月亮,深色时显示太阳
        self.btn_theme.setIcon(
            make_icon("moon" if not dark else "sun", 16)
        )
        self.btn_theme.setToolTip(
            "切换到深色模式" if not dark else "切换到浅色模式"
        )

    def _switch_to_tier(self, tier_id: str, persist: bool = True):
        """切换到指定 tier 的实时工作区页面并(可选)持久化 + 重新注入环境变量。

        懒加载:首次切换到该 tier 时构造页面并加入栈;已有页面复用。

        守卫：录音或后台任务进行中禁止切档——persist 路径会 apply_config()
        重置连接器单例，正在运行的转写 worker 会中途换用新档连接器/模型；
        且各页共享 service.recorder，切走后悬浮窗/热键会转发到无录音的
        新页导致停止入口失效。
        """
        if tier_id not in TIER_ORDER:
            tier_id = TIER_ORDER[0]

        prev_tier = self._current_tier
        # 同档位重复点击也会走 persist 分支重置连接器，一并拦截
        busy = persist and self._any_page_busy()
        if busy:
            self.top_bar.set_active(prev_tier)  # 回弹顶栏选中态
            self.statusBar().showMessage(
                "有录音或转写任务进行中，完成后再切换版本", 4000
            )
            logger.info(f"切档被拒绝（任务进行中）: {prev_tier} → {tier_id}")
            return

        # 懒加载:首次切换到该 tier 时构造页面并加入栈
        page = self._page_instances.get(tier_id)
        if page is None:
            page = make_tier_page(tier_id, self.service, self.cfg)
            self._page_instances[tier_id] = page
            self.stack.insertWidget(_tier_to_index(tier_id), page)
            # 首次构造时连接桥接信号(悬浮窗 / 录音状态 / 转写落库 / 录音会话)
            self._connect_page_signals(page)

        # 切到该 tier 页面
        self.top_bar.set_active(tier_id)
        self.stack.setCurrentWidget(page)
        fade_in(page)  # 200ms 淡入(需求三:视图切换过渡)
        self._in_detail_mode = False
        self._current_tier = tier_id

        # 切换到已构造页面时刷新会议模式 UI(其他页面可能已改 cfg,
        # 本页 UI 控件态需显式同步;首次构造时 __init__ 已调,此处覆盖切换场景)
        try:
            page.apply_meeting_config()
        except Exception as e:
            logger.warning(f"切换 tier 后刷新会议配置失败: {e}")

        t = get_tier(tier_id)
        self.lbl_current_tier.setText(
            f"当前版本:{t.label} — {t.price}  ({t.model})"
        )

        self.cfg.asr_tier = tier_id
        if persist:
            try:
                save_user_config(self.cfg)
                apply_config(self.cfg)  # 重新注入 env + 重置 core 单例/连接器
                self.statusBar().showMessage(f"已切换到「{t.label}」", 3000)
            except Exception as e:
                logger.error(f"切换 tier 失败: {e}")
                self.statusBar().showMessage(f"切换失败:{e}", 5000)
                # 失败回滚：指针 + UI（顶栏高亮/当前页/文案）一并回到 prev，
                # 避免「开启新对话」跳回 prev 页而顶栏仍亮新档的矛盾状态
                self._current_tier = prev_tier
                self.cfg.asr_tier = prev_tier
                self.top_bar.set_active(prev_tier)
                prev_page = self._page_instances.get(prev_tier)
                if prev_page is not None:
                    self.stack.setCurrentWidget(prev_page)
                    t_prev = get_tier(prev_tier)
                    self.lbl_current_tier.setText(
                        f"当前版本:{t_prev.label} — {t_prev.price}  ({t_prev.model})"
                    )

    def _any_page_busy(self) -> bool:
        """任意已构造 tier 页 / 文件转写页是否有录音或后台任务。"""
        for page in self._page_instances.values():
            try:
                if page.is_busy():
                    return True
            except Exception as e:
                logger.warning(f"检查页面忙碌态失败: {e}")
        try:
            if self.upload_page.is_busy():
                return True
        except AttributeError:
            pass
        except Exception as e:
            logger.warning(f"检查文件转写页忙碌态失败: {e}")
        return False

    def _switch_to_upload_page(self) -> None:
        """切到文件转写页（顶栏「文件转写」分段）。"""
        self.top_bar.set_active_upload()
        self.stack.setCurrentWidget(self.upload_page)
        fade_in(self.upload_page)
        self._in_detail_mode = False
        self.statusBar().showMessage("文件转写：上传音频或粘贴路径进行转写", 3000)

    def _switch_to_workspace(self) -> None:
        """切回当前 tier 的实时工作区(从详情视图/文件转写页返回时使用)。"""
        # 若已在当前 tier 工作区,什么都不做
        if not self._in_detail_mode:
            page = self._page_instances.get(self._current_tier)
            if page is not None and self.stack.currentWidget() is page:
                return
        # 重新激活当前 tier
        self._switch_to_tier(self._current_tier, persist=False)

    def _on_session_selected(self, rid: int) -> None:
        """侧边栏选中某条历史 → 切到详情视图 + 加载。"""
        # 若用户切到历史详情时,正在录音,允许详情只读(实时工作区的录音不中断)
        self._in_detail_mode = True
        # 详情视图索引是动态的(懒加载 tier 页会插入到 detail 之前)
        self.stack.setCurrentWidget(self.detail_view)
        fade_in(self.detail_view)
        self.detail_view.load_recording(rid)
        # 同步侧边栏选中态
        self.sidebar.set_active(rid)
        # 状态栏提示
        rec = self.service.get_recording(rid)
        if rec is not None:
            self.statusBar().showMessage(
                f"查看历史:「{rec.title or f'录音 #{rec.id}'}」", 3000
            )

    def _on_session_renamed(self, rid: int, new_title: str) -> None:
        """详情视图改名成功 → 同步刷新侧边栏条目。"""
        self.sidebar.update_recording_title(rid, new_title)

    def _on_sidebar_recording_updated(self, rid: int, new_title: str) -> None:
        """侧边栏就地改名 → 只刷新关联视图，不切换主区（区别于 session_selected）。"""
        if self._in_detail_mode and self.detail_view.current_rid() == rid:
            self.detail_view.refresh_from_db()
        self.statusBar().showMessage(f"已重命名:「{new_title}」", 3000)

    def _on_session_deleted(self, rid: int) -> None:
        """详情视图删除会话 → 刷新侧边栏 + 切回实时工作区。"""
        self.sidebar.remove_recording(rid)
        # 自动切回当前 tier 工作区
        self._switch_to_workspace()
        self.statusBar().showMessage(f"已删除会话 #{rid}", 3000)

    def _on_retrain_requested(self, rid: int) -> None:
        """详情视图请求重新转写。

        实现策略:启动独立 TranscriptionWorker(不切回实时工作区,
        保持详情视图只读;转写完成时刷新详情视图)。
        若已存在 worker,先断开旧信号(避免重复回调)。
        """
        if self._has_running_background_worker():
            logger.info("已有后台任务正在运行,跳过重新转写")
            self.statusBar().showMessage("已有后台任务在运行,请稍后再试", 3000)
            return
        from pc_api.workers import TranscriptionWorker
        self._retrain_worker = TranscriptionWorker(self.service, rid)
        self._retrain_worker.finished_ok.connect(
            lambda r, t: self._after_retrain_done(r, t)
        )
        self._retrain_worker.failed.connect(
            lambda r, e: self._after_retrain_failed(r, e)
        )
        self.statusBar().showMessage(f"正在重新转写 #{rid}…", 5000)
        self._retrain_worker.start()

    def _after_retrain_done(self, rid: int, _text: str) -> None:
        self.detail_view.refresh_from_db()
        self.sidebar.refresh()
        self.statusBar().showMessage(f"#{rid} 重新转写完成", 3000)

    def _after_retrain_failed(self, rid: int, err: str) -> None:
        QMessageBox.warning(self, "重新转写失败", f"#{rid} 转写失败:\n{err}")
        self.statusBar().showMessage(f"#{rid} 重新转写失败", 5000)

    def _on_regen_note_requested(self, rid: int) -> None:
        """详情视图请求重新生成融合笔记。

        启动独立 NoteFuserWorker,完成后刷新详情 + 侧边栏。
        """
        if self._has_running_background_worker():
            self.statusBar().showMessage("已有后台任务在运行,请稍后再试", 3000)
            return
        try:
            fuser = self._get_or_create_note_fuser()
        except ValueError as e:
            QMessageBox.warning(self, "无法生成笔记", str(e))
            return
        from pc_api.workers import NoteFuserWorker
        self._regen_worker = NoteFuserWorker(fuser, rid)
        self._regen_worker.finished_ok.connect(
            lambda r, n: self._after_regen_note_done(r, n)
        )
        self._regen_worker.failed.connect(
            lambda r, e: self._after_regen_note_failed(r, e)
        )
        self.statusBar().showMessage(f"正在为 #{rid} 生成融合笔记…", 5000)
        self._regen_worker.start()

    def _after_regen_note_done(self, rid: int, _note: str) -> None:
        self.detail_view.refresh_from_db()
        self.sidebar.refresh()
        self.statusBar().showMessage(f"#{rid} 融合笔记已生成", 3000)

    def _after_regen_note_failed(self, rid: int, err: str) -> None:
        QMessageBox.warning(self, "生成失败", f"#{rid} 笔记生成失败:\n{err}")
        self.statusBar().showMessage(f"#{rid} 笔记生成失败", 5000)

    def _on_recording_session(self, rid: int, is_recording: bool) -> None:
        """BaseTierPage 通知:录音会话变化(rid+bool)。

        - is_recording=True:  侧边栏添加新条目 + 标红点 + 自动切到工作区
                              (避免详情视图遮挡录音)
        - is_recording=False: 侧边栏清红点
        """
        if is_recording:
            self._active_recording_rid = rid
            # 录音开始 → 强制切回实时工作区(用户可能从详情视图点录音;
            # 但 detail view 没有录音按钮,实际触发路径是用户先点开启新对话
            # 回到工作区后再开始录音;此处兜底,确保录音 UI 可见)
            if self._in_detail_mode:
                self._switch_to_workspace()
            # 刷新侧边栏(让新条目进入列表)+ 标红点
            self.sidebar.refresh()
            self.sidebar.set_recording_rid(rid)
        else:
            # 录音结束:清红点 + 重新刷新(让条目移到正确分组/按完成时间排)
            prev_rid = self._active_recording_rid
            self._active_recording_rid = None
            self.sidebar.set_recording_rid(None)
            if prev_rid is not None:
                self.sidebar.refresh()

    def _on_transcription_saved(self, rid: int, text: str) -> None:
        """BaseTierPage 通知:转写已落库 → 启动 AutoTitleWorker。

        不阻塞 UI;worker 完成后通过 finished_ok/failed 回调刷新侧边栏。
        """
        if self._has_running_background_worker():
            logger.info(f"AutoTitleWorker 已在运行,rid={rid} 跳过本次(避免并发)")
            return
        try:
            fuser = self._get_or_create_note_fuser()
        except ValueError as e:
            logger.info(f"AutoTitleWorker 跳过(无法构造 NoteFuser): {e}")
            return
        worker = AutoTitleWorker(fuser, rid, text)
        worker.progress.connect(self.statusBar().showMessage)
        worker.finished_ok.connect(self._on_auto_title_done)
        worker.failed.connect(self._on_auto_title_failed)
        self._title_worker = worker
        worker.start()
        logger.info(f"AutoTitleWorker 已启动: rid={rid} text_len={len(text)}")

    def _on_auto_title_done(self, rid: int, new_title: str) -> None:
        """AutoTitleWorker 成功 → 改名 + 刷新侧边栏。"""
        self.service.rename_recording(rid, new_title)
        # 刷新侧边栏条目(若在详情视图则一并刷新)
        self.sidebar.update_recording_title(rid, new_title)
        if self._in_detail_mode and self.detail_view.current_rid() == rid:
            self.detail_view.refresh_from_db()
        logger.info(f"AutoTitleWorker 完成: rid={rid} title={new_title!r}")
        self.statusBar().showMessage(f"已自动命名:#{rid} {new_title}", 3000)

    def _on_auto_title_failed(self, rid: int, err: str) -> None:
        """AutoTitleWorker 失败(短文本/无 LLM/异常)→ 静默,保留默认标题。"""
        logger.info(f"AutoTitleWorker 失败 rid={rid}: {err}")

    def _get_or_create_note_fuser(self) -> NoteFuser:
        """懒构造 NoteFuser(无 API key 时构造会抛 ValueError,调用方处理)。"""
        if self._note_fuser is None:
            self._note_fuser = NoteFuser()
        return self._note_fuser

    def _has_running_background_worker(self) -> bool:
        """是否已有转写、笔记或自动命名任务在运行。

        这些任务会写同一条录音记录；限制为单任务可避免用户连点时覆盖
        worker 引用，进而触发 QThread 在运行中被销毁的问题。
        """
        for attr in ("_title_worker", "_retrain_worker", "_regen_worker"):
            worker = getattr(self, attr, None)
            if worker is not None and worker.isRunning():
                return True
        return False

    # ---- 悬浮窗协调 ----

    def _toggle_floating(self) -> None:
        """主窗口 ↔ 悬浮窗 互换可见性(录音不中断)。

        收起主窗口 → 显示悬浮窗;展开主窗口 → 隐藏悬浮窗。
        悬浮窗首次显示时定位到主窗口右上角,避免出现在屏幕外。
        """
        if self.floating.isVisible():
            # 悬浮窗 → 主窗口
            self.floating.hide()
            self.showNormal()
            self.activateWindow()
            self.raise_()
            self._floating_timer.stop()
        else:
            # 主窗口 → 悬浮窗
            # 同步当前录音状态到悬浮窗(可能在录音中收起)
            page = self._current_page()
            is_rec = (
                page is not None
                and page.recording_widget.recording_id() is not None
                and self.service.recorder.is_recording
            )
            self.floating.set_recording_state(is_rec)
            if is_rec:
                self._floating_timer.start()
            # 定位到主窗口右上角(避免首次出现在屏幕外)
            if not self.floating.pos().isNull() and self.floating.pos().x() != 0:
                # 已拖拽过,保持原位
                pass
            else:
                geo = self.geometry()
                self.floating.move(
                    geo.right() - self.floating.width() - 20,
                    geo.top() + 60,
                )
            self.floating.show()
            self.floating.raise_()
            self.hide()

    def _on_floating_start(self) -> None:
        """悬浮窗开始按钮:转发到当前页录音控件。"""
        page = self._current_page()
        if page is not None:
            page.recording_widget.on_start()

    def _on_floating_stop(self) -> None:
        """悬浮窗停止按钮:转发到当前页录音控件。"""
        page = self._current_page()
        if page is not None:
            page.recording_widget.on_stop()

    def _on_floating_pause_toggle(self) -> None:
        """悬浮窗暂停按钮:转发到当前页录音控件。"""
        page = self._current_page()
        if page is not None:
            page.recording_widget.on_pause_toggle()
            self.floating.set_paused_state(self.service.recorder.is_paused)

    def _perform_screenshot(self, rw) -> None:
        """统一截屏入口:悬浮窗可见时先 hide → 延时截屏 → 恢复 show。

        避免截屏捕获到自身悬浮窗(WindowStaysOnTopHint 导致 grabWindow 含悬浮窗
        画面,截图右上角会出现悬浮窗)。QTimer.singleShot(50) 让窗口系统完成一帧
        刷新后再 grabWindow;finally 确保 show 一定执行,避免悬浮窗永久隐藏。

        Args:
            rw: 当前页 RecordingWidget,调其 on_screenshot() 执行截屏
        """
        fw = self.floating
        if fw is not None and fw.isVisible():
            fw.hide()
            def _do():
                try:
                    rw.on_screenshot()
                finally:
                    fw.show()
            QTimer.singleShot(50, _do)
        else:
            rw.on_screenshot()

    def _on_rec_state_changed(self, is_recording: bool) -> None:
        """当前页录音开始/停止 -> 同步悬浮窗状态 + 计时器启停。

        由 BaseTierPage._on_rec_started / _on_rec_stopped emit。
        即使悬浮窗未显示也同步状态,以便下次显示时按钮态正确。
        """
        self.floating.set_recording_state(is_recording)
        if is_recording:
            if self.floating.isVisible():
                self._floating_timer.start()
        else:
            self._floating_timer.stop()
            # 停止时清零音量条(计时保留最后值供查看)
            self.floating.set_volume(0.0)

    def _on_rec_paused_changed(self, is_paused: bool) -> None:
        """当前页录音暂停/恢复 -> 同步悬浮窗暂停按钮态。"""
        self.floating.set_paused_state(is_paused)

    def _update_floating_metrics(self) -> None:
        """200ms 定时器:更新悬浮窗计时 + 音量(仅悬浮窗可见时有效)。"""
        if not self.floating.isVisible():
            return
        page = self._current_page()
        if page is None:
            return
        try:
            elapsed = self.service.recorder.elapsed_seconds()
            self.floating.update_elapsed(elapsed)
        except Exception as e:
            logger.warning(f"更新悬浮窗计时失败: {e}")
        try:
            level = self.service.recorder.current_level()
            self.floating.set_volume(level)
        except Exception as e:
            logger.warning(f"更新悬浮窗音量失败: {e}")

    # ---- 全局热键 ----

    def _register_hotkey(self) -> None:
        """注册 cfg.meeting_hotkey → 截屏。

        优先用 pynput 全局热键(应用失焦也可触发);
        pynput 不可用 / 注册失败 / 热键格式非法 → 回退应用内 QShortcut。
        """
        hotkey = normalize_hotkey(self.cfg.meeting_hotkey)
        if not validate_hotkey(hotkey):
            logger.warning(
                f"热键格式非法: {self.cfg.meeting_hotkey!r},回退默认 ctrl+alt+s"
            )
            hotkey = "ctrl+alt+s"
            self.cfg.meeting_hotkey = hotkey

        # 清理旧的回退 QShortcut：QShortcut 的 C++ 对象以 _hotkey_owner 为父，
        # 仅把 Python 引用置 None 不会销毁它，旧组合键仍会触发且无法注销
        old_shortcut = self._fallback_shortcut
        if old_shortcut is not None:
            try:
                old_shortcut.activated.disconnect()
            except Exception:
                pass
            try:
                old_shortcut.deleteLater()
            except Exception:
                pass
        self._fallback_shortcut = None

        # 注销旧组合键：manager.register 只增不删，换键后旧组合仍会触发截屏。
        # 相同组合则跳过（避免无谓的 listener 重建间隙）。
        old_hotkey = self._registered_hotkey
        if old_hotkey is not None and old_hotkey != hotkey:
            try:
                self._hotkey_mgr.unregister(old_hotkey)
            except Exception as e:
                logger.warning(f"注销旧热键 {old_hotkey} 失败: {e}")
            self._registered_hotkey = None

        # 尝试 pynput 全局热键
        ok = self._hotkey_mgr.register(hotkey, self._hotkey_triggered.emit)
        if ok:
            self._registered_hotkey = hotkey
            self.statusBar().showMessage(f"全局热键已注册: {hotkey}", 3000)
            return

        # 回退 QShortcut(应用内任意窗口活动时生效,主窗口隐藏也不失效)
        try:
            # QKeySequence 用 Ctrl+Alt+S 格式(首字母大写 + 加号)
            qt_seq = QKeySequence(
                "+".join(p.capitalize() for p in hotkey.split("+"))
            )
            # 绑定到常驻隐藏 widget + ApplicationShortcut 上下文:
            # 默认 WindowShortcut 绑定到非窗口 owner 行为未定义;绑定到 self
            # 会在主窗口 hide(悬浮窗模式)后失效。ApplicationShortcut 保证应用
            # 任一窗口活动时即生效(悬浮窗活动也触发)。
            self._fallback_shortcut = QShortcut(qt_seq, self._hotkey_owner)
            self._fallback_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            self._fallback_shortcut.activated.connect(self._on_hotkey_screenshot)
            self.statusBar().showMessage(
                f"全局热键不可用,改用应用内快捷键: {hotkey}", 5000
            )
            logger.info(f"回退 QShortcut(ApplicationShortcut): {hotkey}")
        except Exception as e:
            logger.warning(f"QShortcut 回退也失败: {e}")
            self.statusBar().showMessage("热键注册失败", 5000)

    def _on_hotkey_screenshot(self) -> None:
        """热键触发:转发到当前页录音控件的截屏方法。"""
        page = self._current_page()
        if page is None:
            return
        rw = page.recording_widget
        # 仅在录音中且会议模式开启时响应
        if rw.recording_id() is None:
            self.statusBar().showMessage("未在录音中,热键忽略", 2000)
            return
        if not getattr(rw, "_meeting_mode", False):
            self.statusBar().showMessage("会议模式未开启,热键忽略", 2000)
            return
        self._perform_screenshot(rw)

    def _open_settings(self):
        # 应用设置会 apply_config() 重置全部 core 单例/连接器，
        # 录音或转写进行中打开会造成运行中任务中途换配置
        if self._any_page_busy():
            self.statusBar().showMessage(
                "有录音或转写任务进行中，完成后再修改设置", 4000
            )
            return
        dlg = SettingsDialog(self.cfg, parent=self)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            self.cfg = dlg.get_config()
            try:
                save_user_config(self.cfg)
                apply_config(self.cfg)
            except Exception as e:
                logger.error(f"应用设置失败: {e}")
                self.statusBar().showMessage(f"应用设置失败:{e}", 5000)
                return
            # 通知所有已构造页面刷新 VAD 分段开关。
            # cfg 由 get_config() 原地修改,各页面持有的 self.cfg 引用同步更新,
            # 但 RecordingWidget._segmented_mode 是派生状态,必须显式刷新;
            # 否则下次 on_start 仍按旧 _segmented_mode 走错 start_*_recording 分支。
            for page in self._page_instances.values():
                try:
                    page.apply_vad_config()
                except Exception as e:
                    logger.warning(f"刷新页面 VAD 配置失败: {e}")
                # 同步刷新会议模式配置(开关态/音源/截屏方式/显示器)
                try:
                    page.apply_meeting_config()
                except Exception as e:
                    logger.warning(f"刷新页面会议模式配置失败: {e}")
            # 重新注册全局热键(用户可能改了 hotkey 字段)
            try:
                self._register_hotkey()
            except Exception as e:
                logger.warning(f"重新注册热键失败: {e}")
            # 跳到所选 tier 对应实时工作区页(cfg 已更新,无需再落盘)
            self._switch_to_tier(self.cfg.asr_tier, persist=False)

    def _shutdown_threads(self, workers, timeout_ms: int = 4000) -> None:
        """关闭路径统一停线程：共享超时预算 + 超时 terminate 兜底。

        网络型 worker（AutoTitleWorker 的 LLM 调用 / 重新转写的 SFTP 上传）
        单次 wait(2000) 超时是常态；超时后线程仍在运行而窗口被销毁会触发
        Qt 致命错误「QThread: Destroyed while thread is still running」。
        关闭场景下 terminate 强停比进程 abort 更可取。
        """
        running = [w for w in workers if w is not None and w.isRunning()]
        deadline = time.monotonic() + timeout_ms / 1000.0
        for w in running:
            if hasattr(w, "request_stop"):
                try:
                    w.request_stop()
                except Exception:
                    pass
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms > 0:
                w.wait(remaining_ms)
        for w in running:
            if w.isRunning():
                logger.warning(
                    f"{type(w).__name__} 未在预算内退出，terminate 兜底"
                )
                w.terminate()
                w.wait(1000)

    def closeEvent(self, event):
        # 等待各页面后台 worker 退出。
        # 单页预算收紧为 2.5s：4 个 tier 页 + 上传页串行清理，
        # 共享 deadline 下沉到各页内部，跨页总延迟上限约 15s 而非 44s
        for page in self._page_instances.values():
            try:
                page.cleanup_workers(timeout_ms=2500)
            except Exception as e:
                logger.warning(f"清理 page worker 失败: {e}")
        # 文件转写页 worker
        try:
            self.upload_page.cleanup_workers(timeout_ms=2000)
        except Exception as e:
            logger.warning(f"清理文件转写页 worker 失败: {e}")
        # 关闭悬浮窗 + 停定时器
        try:
            self._floating_timer.stop()
            self.floating.close()
        except Exception as e:
            logger.warning(f"关闭悬浮窗失败: {e}")
        # 停全局热键监听(避免监听线程泄漏)
        try:
            self._hotkey_mgr.stop()
        except Exception as e:
            logger.warning(f"停热键管理器失败: {e}")
        # 停 AutoTitleWorker / 重新转写 / 笔记重生等衍生线程
        self._shutdown_threads([
            self._title_worker,
            getattr(self, "_retrain_worker", None),
            getattr(self, "_regen_worker", None),
        ])
        super().closeEvent(event)


# ============================================================
# 模块级辅助函数
# ============================================================

def _tier_to_index(tier_id: str) -> int:
    """tier_id → MainStack 索引(tier 页 0..len(TIER_ORDER)-1)。"""
    if tier_id in TIER_ORDER:
        return TIER_ORDER.index(tier_id)
    return 0


def get_tier_for_recording(rec) -> Optional[str]:
    """从 Recording 推断 tier_id(若有相关字段)。

    当前 Recording 模型未持久化 tier_id,保留函数便于未来扩展。
    """
    # 优先显式字段(若未来加 asr_tier 列)
    tier_id = getattr(rec, "asr_tier", None)
    if tier_id and tier_id in TIER_ORDER:
        return tier_id
    return None
