"""版本页面基类：承载共享的录音 + 转写 + 笔记逻辑。

各 tier 子类只需覆盖 _build_header() 定制头部卡片（直接调用 _make_tier_header
传入差异化文案与 accent 色即可），主体功能区（录音控件、操作按钮、转写视图、
附件面板、笔记视图）与 worker 调度在本基类统一实现。
"""

import logging
import time
from typing import List, Tuple

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTabWidget,
    QLabel, QSizePolicy, QFrame, QCheckBox, QComboBox,
)

from core.audio.vad_segmenter import VADSegmenter
from core.transcription.tiers import get_tier
from core.user_config import UserConfig
from pc_api.recording_service import RecordingService
from pc_api.gui.recording_widget import RecordingWidget
from pc_api.gui.transcript_view import TranscriptView
from pc_api.gui.attachment_panel import AttachmentPanel
from pc_api.gui.note_view import NoteView
from pc_api.gui.styles import Spinner, Metrics, get_palette
from pc_api.gui.icons import make_icon
from pc_api.gui.toast import Toast
from pc_api.workers import (
    TranscriptionWorker, VLMWorker, NoteFuserWorker, RealtimeTranscriptionWorker,
    SegmentTranscriptionWorker,
)
from pc_api.config_loader import build_realtime_stream_config
from core.fusion import NoteFuser

logger = logging.getLogger(__name__)


class BaseTierPage(QWidget):
    """单个 ASR tier 版本页面。子类覆盖 _build_header() 定制头部。"""

    # VAD 段截断事件桥接信号:从音频线程 QueuedConnection 切到主线程处理
    # (seg_idx, start_s, end_s)
    _segment_queued_signal = Signal(int, float, float)
    # 桥接 RecordingWidget.float_requested → MainWindow._toggle_floating
    # (页面不直接持有悬浮窗,统一由 MainWindow 管理避免双写状态)
    float_requested = Signal()
    # 桥接录音开始/停止 -> MainWindow 同步悬浮窗状态
    # (rid 参数保留以兼容现有信号签名,MainWindow 侧忽略)
    rec_state_changed = Signal(bool)
    # 录音暂停/恢复 -> MainWindow 同步悬浮窗暂停按钮态
    rec_paused_changed = Signal(bool)
    # 会议模式开关/音源在本页变更 → MainWindow 广播到其他已构造页面同步 UI
    # (避免在 A 页开启会议模式后切到 B 页发现开关态/音源不同步)
    meeting_config_changed = Signal()
    # 转写已落库(转写文本+chunks 写入 DB 后 emit)
    # MainWindow 据此启动 AutoTitleWorker,用 LLM 给该会话自动命名。
    # 参数 (recording_id, transcription_text)
    transcription_saved = Signal(int, str)
    # 当前录音会话变化(recording_started / recording_stopped 转给 MainWindow)
    # 参数 (recording_id, is_recording)
    # 0 表示清除(无录音会话);用于侧边栏"录音中 🔴"标识 + 新建录音入库
    recording_session = Signal(int, bool)

    def __init__(self, service: RecordingService, cfg: UserConfig,
                 tier_id: str, parent=None):
        super().__init__(parent)
        self.service = service
        self.cfg = cfg
        self.tier_id = tier_id
        self.tier = get_tier(tier_id)
        self._transcription_worker = None
        self._vlm_worker = None
        self._note_worker = None
        self._note_fuser = None  # 延迟构造（需 API key）
        # realtime tier 专用：实时流式转写 worker + 失败标记
        self._realtime_worker = None
        self._realtime_failed = False
        self._awaiting_realtime_finalize = False  # 停止后等待 realtime 收尾终态
        # VAD 分段模式专用:分段器 + 段 worker 列表 + 计数器
        self._vad: VADSegmenter = None
        self._segment_workers: List[SegmentTranscriptionWorker] = []
        # 已退役但可能仍在运行的 worker（保持 Python 引用，防止 QThread
        # 在线程运行中被垃圾回收析构触发 Qt 致命错误）
        self._retired_workers = []
        self._pending_segments = 0   # 待转写段计数
        self._completed_segments = 0
        self._finalize_pending = False  # 停止后等待段转写完成的标记
        # 录音会话代号：每次开始新录音递增。用于识别旧会话遗留的
        # 定时器回调 / 段 worker 信号（「停止→立即再开始」场景下，
        # 旧回调若不校验会把上一场录音的数据污染进新会话视图）。
        self._record_generation = 0
        self._vad_generation = -1

        self._build_ui()
        self._connect()

        # 根据 cfg 预先初始化分段录音模式开关(必须在录音未开始时设置)。
        # RecordingWidget.on_start 据此决定走 start_segmented_recording
        # 还是 start_recording,从而与下方 VAD 状态机保持一致。
        self.recording_widget.set_segmented_mode(self._is_vad_segmented())

        # 根据 cfg 初始化会议模式开关态 + 音源 + 截屏配置
        # (录音控件已构造,可安全调用 set_meeting_mode 等方法)
        self.apply_meeting_config()

        # VAD 段截断信号 → 主线程处理(音频线程 emit,Qt 自动 Queued)
        self._segment_queued_signal.connect(self._handle_segment_queued)

    def _is_realtime(self) -> bool:
        """是否为实时流式转写 tier（边说边转）。"""
        return self.tier_id == "realtime"

    def _is_vad_segmented(self) -> bool:
        """是否启用 VAD 自动分段(批处理 tier 启用,realtime 不启用)。

        仅在用户开启 vad_enabled 且当前 tier 为批处理(free/pro_lite/pro_pro)时返回 True。
        关闭 VAD 时回退到旧整段转写路径(用户停止后点「开始转写」)。
        """
        if self.tier_id == "realtime":
            return False
        return bool(getattr(self.cfg, "vad_enabled", True))

    def apply_vad_config(self) -> None:
        """根据当前 cfg 重新应用 VAD 分段配置到 RecordingWidget。

        cfg 由 SettingsDialog.get_config() 原地修改,页面持有的 self.cfg 引用同步
        更新,但派生状态 recording_widget._segmented_mode 不会自动跟随 —— 必须在此
        显式刷新。MainWindow 在 SettingsDialog 应用配置后对所有已构造页面调用。

        必须在录音未开始时调用;录音中调用仅记录警告(下次录音生效,本次 recorder
        状态不变,避免与已运行的 VAD/段转写流程冲突)。
        """
        self.recording_widget.set_segmented_mode(self._is_vad_segmented())

    # ---- 头部辅助 ----

    def _make_tier_header(self, accent: str, subtitle: str,
                          features: List[Tuple[str, str]]) -> QFrame:
        """构造统一的 tier 头部卡片。

        Args:
            accent: 装饰条颜色 key（free/realtime/pro_lite/pro_pro），
                    由 QSS 按 [accent="..."] 选择颜色。
            subtitle: 厂商/模型副标题。
            features: [(图标emoji, 文字), ...] 特性条目列表。
        """
        header = QFrame()
        header.setObjectName("TierHeader")
        header.setFrameShape(QFrame.Shape.NoFrame)

        wrap = QVBoxLayout(header)
        wrap.setSpacing(Metrics.SPACING_SM)
        wrap.setContentsMargins(0, 0, 0, 0)

        # 顶部装饰条
        bar = QFrame()
        bar.setObjectName("TierAccentBar")
        bar.setProperty("accent", accent)
        bar.setFixedHeight(4)
        # 触发属性样式刷新
        bar.style().unpolish(bar)
        bar.style().polish(bar)
        wrap.addWidget(bar)

        body = QHBoxLayout()
        body.setSpacing(Metrics.SPACING_MD)
        body.setContentsMargins(
            Metrics.SPACING_LG, Metrics.SPACING_MD,
            Metrics.SPACING_LG, Metrics.SPACING_MD
        )

        # 左：标题 + 副标题
        left = QVBoxLayout()
        left.setSpacing(2)
        title = QLabel(self.tier.label)
        title.setObjectName("tierTitle")
        left.addWidget(title)
        sub = QLabel(subtitle)
        sub.setObjectName("tierSubtitle")
        sub.setWordWrap(True)
        left.addWidget(sub)
        body.addLayout(left, 1)

        # 右：价格徽章
        badge = QLabel(self.tier.price)
        badge.setObjectName("tierPriceBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(badge)

        wrap.addLayout(body)

        # 特性条目行
        if features:
            feat_row = QHBoxLayout()
            feat_row.setSpacing(Metrics.SPACING_SM)
            feat_row.setContentsMargins(
                Metrics.SPACING_LG, 0,
                Metrics.SPACING_LG, Metrics.SPACING_MD
            )
            for _icon, text in features:
                feat = QLabel(text)
                feat.setObjectName("tierFeature")
                feat.setWordWrap(True)
                feat_row.addWidget(feat, 1)
            wrap.addLayout(feat_row)

        return header

    # ---- UI ----

    # Tab 索引常量（避免魔法数字散落各处）
    TAB_TRANSCRIPT = 0  # 实时转写
    TAB_NOTE = 1        # 融合笔记
    TAB_ATTACHMENTS = 2 # 照片附件

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(Metrics.SPACING_MD)
        outer.setContentsMargins(
            Metrics.SPACING_LG, Metrics.SPACING_LG,
            Metrics.SPACING_LG, Metrics.SPACING_MD
        )

        # 1. 头部（子类覆盖）
        outer.addWidget(self._build_header())

        # 1.5 会议模式开关行（视频会议场景：系统音频 + 截屏 + 悬浮窗）
        outer.addWidget(self._build_meeting_bar())

        # 2. 录音控件
        self.recording_widget = RecordingWidget(self.service)
        outer.addWidget(self.recording_widget)

        # 3. 操作按钮行（紧贴 Tab 之上，与下方工作区解耦）
        action_bar = QHBoxLayout()
        action_bar.setSpacing(Metrics.SPACING_SM)

        self.btn_transcribe = QPushButton("开始转写 + 图片描述")
        self.btn_transcribe.setObjectName("primaryButton")
        self.btn_transcribe.setIcon(make_icon("play", 18, get_palette().TEXT_ON_PRIMARY))
        self.btn_transcribe.setEnabled(False)

        self.btn_generate_note = QPushButton("生成融合笔记")
        self.btn_generate_note.setObjectName("primaryButton")
        self.btn_generate_note.setIcon(make_icon("note", 18, get_palette().TEXT_ON_PRIMARY))
        self.btn_generate_note.setEnabled(False)

        self.spinner = Spinner(size=22)
        self.spinner.setVisible(False)
        self.spinner.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.lbl_action_status = QLabel("")
        self.lbl_action_status.setObjectName("subtitleLabel")
        # 限宽避免长文案挤压左侧按钮；超出由 stretch 优先吸收
        self.lbl_action_status.setMaximumWidth(360)

        action_bar.addWidget(self.btn_transcribe)
        action_bar.addWidget(self.btn_generate_note)
        action_bar.addStretch()
        action_bar.addSpacing(Metrics.SPACING_MD)
        action_bar.addWidget(self.spinner)
        action_bar.addWidget(self.lbl_action_status)
        outer.addLayout(action_bar)

        # 4. 工作区 Tab：转写 / 笔记 / 附件（三块各占全宽，不再共用一行）
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)  # 配合 QSS 的扁平样式
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)

        # Tab 0: 实时转写
        self.transcript_view = TranscriptView()
        self.tabs.addTab(self.transcript_view, "实时转写")

        # Tab 1: 融合笔记
        self.note_view = NoteView()
        self.tabs.addTab(self.note_view, "融合笔记")

        # Tab 2: 照片附件（标签上的数量在新增/清空时更新）
        self.attachment_panel = AttachmentPanel()
        self.tabs.addTab(self.attachment_panel, "照片附件 (0)")

        outer.addWidget(self.tabs, 1)

        # realtime tier：转写在录音时自动进行，隐藏「开始转写」按钮
        # （失败回退时由 _on_realtime_failed 显露）
        if self._is_realtime():
            self.btn_transcribe.setVisible(False)

    def _build_header(self) -> QWidget:
        """子类必须覆盖：返回该 tier 的定制头部 widget。"""
        raise NotImplementedError

    def _build_meeting_bar(self) -> QFrame:
        """会议模式开关行：[会议模式] [音源▾] [悬浮窗]。

        会议模式与 ASR tier 正交：只管采集源（系统音频/混音/麦克风）+ 截屏 + 悬浮窗。
        开关态由 cfg.meeting_mode 持久化,音源由 cfg.meeting_audio_source 持久化。
        """
        bar = QFrame()
        bar.setObjectName("Card")
        bar.setFrameShape(QFrame.Shape.NoFrame)

        layout = QHBoxLayout(bar)
        layout.setSpacing(Metrics.SPACING_SM)
        layout.setContentsMargins(
            Metrics.SPACING_MD, Metrics.SPACING_SM,
            Metrics.SPACING_MD, Metrics.SPACING_SM
        )

        self.chk_meeting = QCheckBox("会议模式")
        self.chk_meeting.setToolTip(
            "视频会议场景：录制系统音频 + 麦克风，支持截屏与悬浮窗"
        )
        self.chk_meeting.toggled.connect(self._on_meeting_toggled)

        lbl_audio_source = QLabel("音源")
        lbl_audio_source.setObjectName("subtitleLabel")
        self.combo_audio_source = QComboBox()
        self.combo_audio_source.addItem("系统声音 + 麦克风", "mixed")
        self.combo_audio_source.addItem("仅系统声音", "system")
        self.combo_audio_source.addItem("仅麦克风", "mic")
        self.combo_audio_source.currentIndexChanged.connect(
            self._on_audio_source_changed
        )
        self.combo_audio_source.setEnabled(False)  # 关闭会议模式时禁用

        # 悬浮窗按钮（与 RecordingWidget 的 btn_float 作用一致,
        # 但放在会议模式行更直观,任意模式可用）
        self.btn_float_bar = QPushButton("悬浮窗")
        self.btn_float_bar.setObjectName("toolButton")
        self.btn_float_bar.setIcon(make_icon("float", 16))
        self.btn_float_bar.setToolTip("收起为悬浮小窗（录音不中断）")
        self.btn_float_bar.clicked.connect(self.float_requested.emit)

        layout.addWidget(self.chk_meeting)
        layout.addSpacing(Metrics.SPACING_MD)
        layout.addWidget(lbl_audio_source)
        layout.addWidget(self.combo_audio_source)
        layout.addStretch()
        layout.addWidget(self.btn_float_bar)
        return bar

    # ---- 会议模式编排 ----

    def apply_meeting_config(self) -> None:
        """根据当前 cfg 重新应用会议模式配置到 UI 与 RecordingWidget。

        由 MainWindow 在 SettingsDialog 应用配置后对所有已构造页面调用,
        以及在 meeting_config_changed 信号广播时对其他页面调用
        (仿 apply_vad_config 模式)。cfg 由 get_config() 原地修改,
        页面持有的 self.cfg 引用同步更新,但 UI 控件态与 RecordingWidget 的
        派生状态不会自动跟随 —— 必须在此显式刷新。

        可在录音中调用(仅影响 UI 开关态与下次录音的音源,不改变当前录音)。
        """
        # 同步开关态(避免触发 _on_meeting_toggled 的写回逻辑,先 block 信号)
        was_blocked = self.chk_meeting.blockSignals(True)
        try:
            self.chk_meeting.setChecked(bool(self.cfg.meeting_mode))
        finally:
            self.chk_meeting.blockSignals(was_blocked)

        # 同步音源下拉(block 防 setCurrentIndex 触发 _on_audio_source_changed
        # → meeting_config_changed → 广播回环)
        was_combo_blocked = self.combo_audio_source.blockSignals(True)
        try:
            self._set_combo_by_data(self.combo_audio_source,
                                    self.cfg.meeting_audio_source, "mixed")
        finally:
            self.combo_audio_source.blockSignals(was_combo_blocked)
        self.combo_audio_source.setEnabled(bool(self.cfg.meeting_mode))

        # 同步 RecordingWidget 的会议模式态 + 音源 + 截屏配置
        rw = self.recording_widget
        rw.set_meeting_mode(bool(self.cfg.meeting_mode))
        rw.set_meeting_audio_source(self.cfg.meeting_audio_source)
        rw.set_screenshot_config(
            self.cfg.meeting_screenshot_mode,
            getattr(self.cfg, "meeting_screen_index", 0),
        )

    def _on_meeting_toggled(self, enabled: bool) -> None:
        """会议模式开关切换:联动 UI + RecordingWidget + 写回 cfg(不立即落盘)。

        下次切换 tier / 打开设置对话框时由 save_user_config 落盘。
        末尾 emit meeting_config_changed → MainWindow 广播到其他已构造页面同步。
        """
        self.combo_audio_source.setEnabled(enabled)
        rw = self.recording_widget
        rw.set_meeting_mode(enabled)
        # 音源跟随当前下拉值(默认 mixed)
        source = self.combo_audio_source.currentData() or "mixed"
        rw.set_meeting_audio_source(source)
        # 写回 cfg
        self.cfg.meeting_mode = enabled
        self.cfg.meeting_audio_source = source
        # 状态提示
        if enabled:
            self._show_status(
                f"会议模式已开启（音源：{self.combo_audio_source.currentText()}）"
            )
        else:
            self._show_status("会议模式已关闭")
        # 广播到其他页面同步开关态/音源
        self.meeting_config_changed.emit()

    def _on_audio_source_changed(self) -> None:
        """音源下拉切换:写回 cfg + 同步 RecordingWidget。

        末尾 emit meeting_config_changed → MainWindow 广播到其他已构造页面同步。
        """
        source = self.combo_audio_source.currentData() or "mixed"
        self.cfg.meeting_audio_source = source
        self.recording_widget.set_meeting_audio_source(source)
        if self.chk_meeting.isChecked():
            self._show_status(f"音源已切换：{self.combo_audio_source.currentText()}")
        # 广播到其他页面同步音源下拉
        self.meeting_config_changed.emit()

    @staticmethod
    def _set_combo_by_data(combo, target_data, fallback) -> None:
        """按 itemData 选中 QComboBox,未命中则选 fallback 对应项。"""
        for i in range(combo.count()):
            if combo.itemData(i) == target_data:
                combo.setCurrentIndex(i)
                return
        for i in range(combo.count()):
            if combo.itemData(i) == fallback:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def _connect(self):
        rw = self.recording_widget
        rw.status_message.connect(self._show_status)
        rw.recording_started.connect(self._on_rec_started)
        rw.recording_stopped.connect(self._on_rec_stopped)
        rw.recording_paused.connect(lambda: self.rec_paused_changed.emit(True))
        rw.recording_resumed.connect(lambda: self.rec_paused_changed.emit(False))
        rw.photo_added.connect(self._on_photo_added)
        rw.float_requested.connect(self.float_requested.emit)
        self.btn_transcribe.clicked.connect(self._on_transcribe)
        self.btn_generate_note.clicked.connect(self._on_generate_note)

    def _show_status(self, msg: str):
        win = self.window()
        if hasattr(win, "statusBar"):
            win.statusBar().showMessage(msg)

    def _show_toast(self, msg: str, duration: int = 2000) -> None:
        """柔和 toast 通知（替代状态栏的频繁过程提示）。"""
        win = self.window()
        if win is not None:
            Toast.show(win, msg, duration)

    # ---- 录音事件 ----

    def _on_rec_started(self, rid: int):
        self._record_generation += 1
        self.transcript_view.clear()
        self.attachment_panel.clear()
        self.note_view.clear()
        self.btn_transcribe.setEnabled(False)
        self.btn_generate_note.setEnabled(False)
        self._set_action_status("", loading=False)
        self._realtime_failed = False
        self._awaiting_realtime_finalize = False
        # 上一场录音遗留、可能仍在运行的段 worker 移入退役列表保引用
        if self._segment_workers:
            self._retired_workers.extend(self._segment_workers)
        # VAD 模式状态复位（不触碰旧 worker 线程本身，仅换新列表）
        self._segment_workers = []
        self._pending_segments = 0
        self._completed_segments = 0
        self._finalize_pending = False
        self._vad = None
        # 录音中锁定会议模式开关/音源下拉:切换音源需重建音频流,录音中切换会
        # 导致崩溃;会议模式开关改变按钮可见性也易让用户误以为音源已切。
        self.chk_meeting.setEnabled(False)
        self.combo_audio_source.setEnabled(False)
        # 通知 MainWindow 同步悬浮窗按钮态(开始录音 → 悬浮窗切到停止态)
        self.rec_state_changed.emit(True)
        # 通知 MainWindow:当前录音会话开始 → 侧边栏标红点 + 接收新条目
        self.recording_session.emit(rid, True)

        # realtime tier：录音开始即启动实时流式转写（边说边转）
        if self._is_realtime():
            self._start_realtime_worker(rid)
            return

        # 批处理 tier + VAD 启用:启动 VAD 状态机
        # (分段录音模式已在 __init__ / apply_vad_config 时通过 set_segmented_mode
        # 预先写入 RecordingWidget,on_start 据此走 start_segmented_recording;
        # 此处若再调用 set_segmented_mode 会因 is_recording=True 触发"下次生效"警告,
        # 且与已启动的录音器状态不一致 → rotate_segment 抛 RuntimeError)
        if self._is_vad_segmented():
            self._start_vad_segmenter(rid)
            return

        # 传统批处理(用户关闭了 VAD):走旧路径,等用户停止后点「开始转写」

    def _on_rec_stopped(self, rid: int):
        # 通知 MainWindow 同步悬浮窗按钮态(停止录音 → 悬浮窗切到开始态)
        # 无论后续走 realtime/VAD/传统分支,录音已停止这一事实先同步
        self.rec_state_changed.emit(False)
        # 通知 MainWindow:录音会话结束 → 侧边栏移除红点
        # 保留 rid(0 表示清除)以备后续按 rid 定位条目
        self.recording_session.emit(rid, False)
        # 恢复会议模式开关(音源下拉的 enabled 跟随会议模式开关态,
        # 与 _on_meeting_toggled 第 364 行一致)
        self.chk_meeting.setEnabled(True)
        self.combo_audio_source.setEnabled(bool(self.cfg.meeting_mode))
        if self._is_realtime():
            w = self._realtime_worker
            if w is not None and w.isRunning():
                # worker 仍在跑 → 请求停止，等 finished_ok / finalize_done 回调统一收尾
                self._awaiting_realtime_finalize = True
                w.request_stop()
                self._set_action_status("正在整理实时转写…", loading=True)
                self._show_status("正在整理转写…")
                self.btn_generate_note.setEnabled(False)
                return
            # worker 已退出（失败回退）→ 走文件转写路径
            self._realtime_worker = None
            self.btn_transcribe.setEnabled(True)
            self.btn_generate_note.setEnabled(False)
            self._show_status("实时转写已停止，可点「开始转写」走文件转写")
            return

        if self._is_vad_segmented() and self._vad is not None:
            # VAD 模式:停止 VAD + 处理最后段 + 等所有段 worker 完成
            self._stop_vad_and_finalize(rid)
            return

        # 传统批处理分支（free/pro_lite/pro_pro 且 VAD 关闭）
        self.btn_transcribe.setEnabled(True)
        self.btn_generate_note.setEnabled(False)
        self._show_status("录音已停止，点击「开始转写 + 图片描述」")

    def _on_photo_added(self, att_id: int):
        rid = self.recording_widget.recording_id()
        if rid:
            self._refresh_attachments(rid)

    def _refresh_attachments(self, rid: int):
        self.attachment_panel.clear()
        atts = self.service.list_attachments(rid)
        for a in atts:
            try:
                img_path = self.service.resolve_local_path(a.storage_locator)
                # 兼容旧数据：source 为 None 时按 camera 兜底
                source = getattr(a, "source", None) or "camera"
                self.attachment_panel.add_attachment(
                    a.id, img_path, a.timestamp, a.description or "", source=source
                )
            except Exception as e:
                logger.warning(f"刷新附件失败 att={a.id}: {e}")
        # 同步 Tab 标题上的数量
        self._update_attachment_tab_title()

    def _current_work_rid(self) -> int:
        """获取当前录音会话 recording_id(文件转写已迁移到独立页面)。"""
        return self.recording_widget.recording_id()

    # ---- 转写 ----

    def _on_transcribe(self):
        rid = self.recording_widget.recording_id()
        if not rid:
            return
        self.btn_transcribe.setEnabled(False)
        self.btn_generate_note.setEnabled(False)
        self.transcript_view.set_loading("转写中…")
        self._set_action_status("转写中…", loading=True)
        self._show_status("转写中…")

        self._swap_singleton_worker("_transcription_worker",
                                    TranscriptionWorker(self.service, rid))
        self._transcription_worker.progress.connect(self._show_status)
        self._transcription_worker.finished_ok.connect(self._on_transcribe_done)
        self._transcription_worker.failed.connect(self._on_transcribe_failed)
        self._transcription_worker.start()

    def _swap_singleton_worker(self, attr: str, new_worker):
        """替换单例 worker 引用：运行中的旧值先退役，防 GC 析构崩溃。"""
        old = getattr(self, attr, None)
        if old is not None and old.isRunning():
            self._retired_workers.append(old)
        setattr(self, attr, new_worker)

    def _on_transcribe_done(self, rid: int, text: str):
        self._after_transcription_ready(rid, text)

    def _after_transcription_ready(self, rid: int, text: str) -> None:
        """转写落库后的统一收尾：展示文本 + 启动 VLM 或启用笔记按钮。

        批处理（_on_transcribe_done）与实时（_on_realtime_finished）两条路径
        复用，避免逻辑重复。
        """
        rec = self.service.get_recording(rid)
        chunks = self.service.list_chunks(rid)
        self.transcript_view.set_transcription(
            rec.transcription if rec and rec.transcription else text, chunks
        )

        # 通知 MainWindow:转写已落库 → 触发 LLM 自动命名(AutoTitleWorker)
        # 用 DB 中保存的文本(优先)或回退的 text,确保 AutoTitleWorker 拿到的是
        # 落库后真实内容(分块拼接/清洗后的版本)
        saved_text = rec.transcription if rec and rec.transcription else text
        self.transcription_saved.emit(rid, saved_text)

        if not self.attachment_panel.has_attachments():
            self._show_toast("转写完成，可生成融合笔记")
            self._set_action_status("转写完成", loading=False)
            self.btn_transcribe.setEnabled(True)
            self.btn_generate_note.setEnabled(True)
            return

        self._launch_vlm(rid)

    def _launch_vlm(self, rid: int) -> None:
        """启动 VLM 描述 worker，批处理与实时两条路径复用。"""
        self._show_status("转写完成，开始生成图片描述…")
        self._set_action_status("生成图片描述中…", loading=True)
        self._swap_singleton_worker("_vlm_worker", VLMWorker(self.service, rid))
        self._vlm_worker.progress.connect(lambda aid, m: self._show_status(m))
        self._vlm_worker.finished_ok.connect(self._on_vlm_done)
        self._vlm_worker.failed.connect(self._on_vlm_failed)
        self._vlm_worker.all_done.connect(self._on_vlm_all_done)
        self._vlm_worker.start()

    def _on_transcribe_failed(self, rid: int, err: str):
        self.transcript_view.set_loading(f"转写失败：{err}")
        self._set_action_status("转写失败", loading=False)
        self.btn_transcribe.setEnabled(True)

    # ---- 实时流式转写（realtime tier 专用）----

    def _start_realtime_worker(self, rid: int) -> None:
        """启动实时转写 worker，订阅麦克风音频帧，边说边转。"""
        config = build_realtime_stream_config()
        if not config.get("api_key"):
            self._on_realtime_failed("未配置 DashScope API key")
            return
        self._realtime_worker = RealtimeTranscriptionWorker(
            self.service, rid, self.service.recorder, config
        )
        # 记录本 worker 所属会话代号：停止→快速重开的窗口内，旧 worker 的
        # 迟到回调（session.stop() join 可达数秒）凭此识别并丢弃，
        # 防止清掉新 worker 引用或把旧会话结果渲染进新视图
        self._realtime_generation = self._record_generation
        gen = self._realtime_generation
        self._realtime_worker.partial_text.connect(self._on_live_partial)
        self._realtime_worker.final_sentence.connect(self._on_live_final)
        self._realtime_worker.status.connect(self._show_status)
        self._realtime_worker.failed.connect(self._on_realtime_failed)
        self._realtime_worker.finished_ok.connect(self._on_realtime_finished)
        self._realtime_worker.finalize_done.connect(
            lambda r, g=gen: self._on_realtime_finalize_done(r, g))
        self.transcript_view.set_loading("实时转写连接中…")
        self._realtime_worker.start()

    def _on_live_partial(self, text: str) -> None:
        """当前生长句中间结果（灰斜体实时刷新）。"""
        self.transcript_view.set_partial(text)

    def _on_live_final(self, text: str, begin_s: float, end_s: float) -> None:
        """最终句落地（黑字 + 时间戳）。"""
        self.transcript_view.append_final(text, begin_s, end_s)

    def _on_realtime_failed(self, err: str) -> None:
        """实时流失败：显露「开始转写」按钮供文件转写回退，录音不受影响。"""
        self._realtime_failed = True
        self.transcript_view.set_loading(
            f"实时转写失败：{err}\n停止录音后可点「开始转写」走文件转写")
        self._set_action_status("实时转写失败，可改用文件转写", loading=False)
        # 显露并启用转写按钮（停止后可点）；通知 worker 退出 wait。
        # 注意必须同时 setEnabled：录音开始时按钮被 disable，
        # 仅 setVisible 会留下「可见但不可点」的死按钮。
        self.btn_transcribe.setVisible(True)
        self.btn_transcribe.setEnabled(True)
        w = self._realtime_worker
        if w is not None and w.isRunning():
            w.request_stop()

    def _on_realtime_finished(self, rid: int, text: str) -> None:
        """实时转写停止并落库成功：统一收尾（展示文本 + VLM/笔记）。"""
        if self._realtime_generation != self._record_generation:
            logger.warning(
                f"忽略旧会话 realtime 完成回调 rid={rid}(gen={self._realtime_generation})")
            return
        self._awaiting_realtime_finalize = False
        self._retire_worker(self._realtime_worker)
        self._realtime_worker = None
        self._after_transcription_ready(rid, text)

    def _on_realtime_finalize_done(self, rid: int, gen: int = -1) -> None:
        """realtime 停止阶段收尾终态（成功路径也会触发，此处幂等处理）。

        覆盖 aborted / 停止阶段异常等无 finished_ok 的路径：
        退出「正在整理实时转写…」等待态，恢复按钮可用。
        gen 校验同 _on_realtime_finished：旧会话迟到回调不碰当前状态。
        """
        if gen != -1 and gen != self._record_generation:
            # 旧会话 worker：仅退役引用，不触碰新会话状态
            logger.warning(f"忽略旧会话 realtime 收尾回调 rid={rid}(gen={gen})")
            return
        worker = self._realtime_worker
        self._retire_worker(worker)
        self._realtime_worker = None
        if not self._awaiting_realtime_finalize:
            # 成功路径已由 _on_realtime_finished 收尾
            return
        self._awaiting_realtime_finalize = False
        logger.warning(f"realtime 收尾未产出结果 rid={rid}，回退文件转写")
        self.transcript_view.set_loading(
            "实时转写未产出结果\n可点「开始转写」走文件转写")
        self._set_action_status("实时转写未完成，可改用文件转写", loading=False)
        self.btn_transcribe.setVisible(True)
        self.btn_transcribe.setEnabled(True)
        self.btn_generate_note.setEnabled(False)
        self._show_status("实时转写已停止，可点「开始转写」走文件转写")

    # ---- VAD 自动分段(批处理 tier: free/pro_lite/pro_pro) ----

    def _effective_vad_threshold(self) -> float:
        """根据当前音源选择 VAD 静音阈值。

        系统音频电平特性与麦克风不同(静音时可能恒为0、有声音时信号爆表),
        复用麦克风阈值会导致乱切/不切。会议模式 + system/mixed 音源用独立阈值。
        """
        cfg = self.cfg
        is_meeting_audio = (
            cfg.meeting_mode
            and cfg.meeting_audio_source in ("system", "mixed")
        )
        if is_meeting_audio:
            return cfg.meeting_vad_silence_threshold
        return cfg.vad_silence_threshold

    def _start_vad_segmenter(self, rid: int) -> None:
        """启动 VAD 分段器,订阅麦克风音频帧。

        依赖 RecordingWidget.on_start 已通过 set_segmented_mode(True) 启动分段录音
        (开第 0 段 wav)。VAD 状态机收到音频帧后,在静音达阈值时 emit 段截断信号。
        """
        cfg = self.cfg
        threshold = self._effective_vad_threshold()
        self._vad = VADSegmenter(
            silence_threshold=threshold,
            silence_duration=cfg.vad_silence_duration,
            min_segment_seconds=cfg.vad_min_segment,
            max_segment_seconds=cfg.vad_max_segment,
            on_segment_finalize=self._on_vad_segment_finalize,
            sample_rate=self.service.recorder.SAMPLE_RATE,  # 与录音器同源,确保帧→秒换算一致
        )
        # 记录本 VAD 所属录音代号：停止→立即再开始时，旧 VAD 遗留的
        # 截断信号凭此识别并丢弃，避免污染新会话
        self._vad_generation = self._record_generation
        rec_start = self.service.recorder.recording_start_time
        if rec_start is None:
            logger.error("VAD 启动失败:recorder.recording_start_time 为 None")
            self._show_status("VAD 启动失败:录音未开始")
            return
        # recording_start_time 仅日志用;状态机基于音频帧累计时间,与墙钟解耦
        self._vad.start(rec_start)
        # 订阅音频帧(VAD.consume_frame 在音频线程调用)
        self.service.recorder.add_subscriber(self._vad.consume_frame)
        # 阈值来源标注,便于标定调试
        threshold_src = (
            "meeting_vad" if cfg.meeting_mode
            and cfg.meeting_audio_source in ("system", "mixed")
            else "vad(麦克风)"
        )
        self.transcript_view.set_loading("VAD 分段中…说话停顿后自动转写")
        self._show_toast(
            f"VAD 已启动（静音 {cfg.vad_silence_duration}s 触发自动转写）",
            duration=2500,
        )
        logger.info(
            f"VAD 启动: rec={rid} silence={cfg.vad_silence_duration}s "
            f"threshold={threshold} src={threshold_src}"
        )

    def _on_vad_segment_finalize(self, seg_idx: int, start_s: float,
                                  end_s: float) -> None:
        """VAD 触发段截断回调(在音频线程调用,必须非阻塞)。

        通过 Qt 信号 QueuedConnection 切回主线程处理(避免在音频线程
        操作 worker / DB)。"""
        self._segment_queued_signal.emit(seg_idx, start_s, end_s)

    def _handle_segment_queued(self, seg_idx: int, start_s: float,
                                end_s: float) -> None:
        """主线程处理段截断:rotate WAV + 落库 + 启动段转写 worker。

        注意:seg_idx 来自 VAD 状态机,与 AudioRecorder._segment_index 对齐
        (VAD 截断时录音器仍在写当前段,rotate_segment 关闭该段并开下一段)。
        """
        # 旧会话遗留的截断信号（停止后立即再开始的窗口期）直接丢弃，
        # 否则会对新会话误执行 rotate_segment / 串入旧音频
        if self._vad_generation != self._record_generation:
            logger.warning(
                f"丢弃旧会话段截断信号 seg={seg_idx} "
                f"(gen={self._vad_generation} != {self._record_generation})"
            )
            return
        rid = self.recording_widget.recording_id()
        if rid is None:
            logger.warning(f"段 {seg_idx} 截断回调:无活动 recording_id,跳过")
            return
        try:
            # rotate WAV(关闭当前段 wav,开下一段)
            seg_path, _ = self.service.recorder.rotate_segment()
            duration = end_s - start_s
            seg_id = self.service.add_audio_segment(
                rid, seg_idx, seg_path, start_s, end_s, duration
            )
            self._pending_segments += 1
            worker = SegmentTranscriptionWorker(
                self.service, rid, seg_idx, seg_id, seg_path
            )
            gen = self._record_generation
            worker.chunk_done.connect(
                lambda r, i, meta, g=gen: self._on_segment_chunk_done(r, i, meta, g))
            worker.segment_failed.connect(
                lambda r, i, err, g=gen: self._on_segment_failed(r, i, err, g))
            worker.status.connect(
                lambda msg, g=gen: self._show_status(msg)
                if g == self._record_generation else None)
            self._segment_workers.append(worker)
            worker.start()
            # 转写"悄悄进行"：段级提示不打扰，仅用 toast 轻量提示
            self._show_toast(f"段 {seg_idx} 已截断，自动转写中…")
        except Exception as e:
            logger.exception(f"段 {seg_idx} 入队失败")
            self._show_status(f"段 {seg_idx} 入队失败: {e}")

    def _on_segment_chunk_done(self, rid: int, seg_idx: int,
                                chunks_meta: list, gen: int = -1) -> None:
        """单段转写完成:把 chunks 依次 append 到 TranscriptView(模拟 realtime)。

        chunks_meta 内每项 (text, start_s, end_s) 已加段起始偏移,直接显示。
        gen 为创建 worker 时的会话代号：不匹配说明是旧会话遗留回调，
        DB 已落库但 UI 不再追加（避免污染当前视图与计数）。
        """
        if gen != -1 and gen != self._record_generation:
            logger.warning(
                f"忽略旧会话段完成回调 seg={seg_idx}(gen={gen})")
            return
        self._completed_segments += 1
        for text, start, end in chunks_meta:
            if not text or not text.strip():
                continue
            self.transcript_view.append_final(text, start, end)
        # 清除 loading 提示(若还在)
        if self._pending_segments > 0 and self._completed_segments >= self._pending_segments \
                and not self._finalize_pending:
            self._set_action_status(
                f"已转写 {self._completed_segments} 段", loading=False
            )
        else:
            self._set_action_status(
                f"段转写中({self._completed_segments}/{self._pending_segments})…",
                loading=self._finalize_pending,
            )

    def _on_segment_failed(self, rid: int, seg_idx: int, err: str,
                           gen: int = -1) -> None:
        """单段转写失败:计入完成数(避免卡死),提示用户但不中断其他段。

        gen 校验同 _on_segment_chunk_done：旧会话遗留回调不污染当前视图。
        """
        if gen != -1 and gen != self._record_generation:
            logger.warning(f"忽略旧会话段失败回调 seg={seg_idx}(gen={gen})")
            return
        logger.error(f"段 {seg_idx} 转写失败: {err}")
        self._completed_segments += 1
        self._show_status(f"段 {seg_idx} 转写失败: {err}")
        self.transcript_view.append_final(
            f"[段 {seg_idx} 转写失败: {err}]", None, None
        )

    def _stop_vad_and_finalize(self, rid: int) -> None:
        """录音停止收尾:停 VAD 订阅 + 处理最后一段 + 等所有段 worker 完成。

        RecordingWidget.on_stop 已调用 service.stop_recording()(关闭最后一段
        wav 并把路径存到 recorder.last_segment_path)。此处不重复 stop_recording。
        """
        # 1. 停 VAD 订阅
        if self._vad is not None:
            try:
                self.service.recorder.remove_subscriber(self._vad.consume_frame)
            except Exception:
                pass
            last = self._vad.stop()
            self._vad = None
        else:
            last = None

        # 2. 处理最后一段(若 VAD 状态机有活动段且达到最小段长)
        # 注意:RecordingWidget.on_stop 已调 stop_recording 关闭最后段 wav,
        # recorder.last_segment_path 是刚关闭的最后段路径。
        if last is not None:
            seg_idx, start_s, end_s = last
            duration = end_s - start_s
            if duration >= self.cfg.vad_min_segment:
                last_path = self.service.recorder.last_segment_path
                if last_path:
                    try:
                        seg_id = self.service.add_audio_segment(
                            rid, seg_idx, last_path, start_s, end_s, duration
                        )
                        self._pending_segments += 1
                        worker = SegmentTranscriptionWorker(
                            self.service, rid, seg_idx, seg_id, last_path
                        )
                        gen = self._record_generation
                        worker.chunk_done.connect(
                            lambda r, i, meta, g=gen: self._on_segment_chunk_done(r, i, meta, g))
                        worker.segment_failed.connect(
                            lambda r, i, err, g=gen: self._on_segment_failed(r, i, err, g))
                        worker.status.connect(
                lambda msg, g=gen: self._show_status(msg)
                if g == self._record_generation else None)
                        self._segment_workers.append(worker)
                        worker.start()
                        self._show_status(f"末段 {seg_idx} 入队转写…")
                    except Exception as e:
                        logger.exception(f"末段 {seg_idx} 入队失败")
                        self._show_status(f"末段入队失败: {e}")
                else:
                    logger.warning(f"末段 {seg_idx} 无 last_segment_path,跳过")
            else:
                logger.info(f"末段 {seg_idx} 过短({duration:.1f}s),丢弃")

        # 3. 等待所有段 worker 完成 → 收尾
        self._finalize_pending = True
        self._set_action_status(
            f"等待段转写完成({self._completed_segments}/"
            f"{self._pending_segments})…",
            loading=True,
        )
        # 用 QTimer 轮询(每 500ms 检查一次是否全部完成)。
        # 捕获当前会话代号：若用户在轮询期间又开始新录音，旧轮询链凭此自毁，
        # 避免把旧会话的转写结果覆盖进新会话视图。
        gen = self._record_generation
        QTimer.singleShot(500, lambda: self._poll_workers_finalize(rid, gen))

    def _poll_workers_finalize(self, rid: int, gen: int) -> None:
        """轮询所有段 worker 是否完成,完成后触发收尾进入 VLM/笔记流程。"""
        if gen != self._record_generation:
            # 会话已切换：本收尾链属于上一场录音，作废
            logger.warning(f"作废旧会话收尾轮询 rid={rid} gen={gen}")
            return
        pending = [w for w in self._segment_workers if w.isRunning()]
        self._set_action_status(
            f"等待段转写完成({self._completed_segments}/"
            f"{self._pending_segments})…",
            loading=True,
        )
        if pending:
            QTimer.singleShot(500, lambda: self._poll_workers_finalize(rid, gen))
            return

        # 全部完成 → 收尾
        self._finalize_pending = False
        # 取录音总时长(从 RecordingService 取或 recorder.elapsed)
        rec = self.service.get_recording(rid)
        total_duration = (rec.audio_duration_seconds if rec
                          and rec.audio_duration_seconds else 0.0)
        try:
            self.service.finalize_segmented_recording(rid, total_duration)
        except Exception as e:
            logger.exception(f"finalize_segmented_recording 失败: {e}")

        # 用 DB 中累积的 chunks 与 transcription 重渲染(统一时间戳格式)
        chunks = self.service.list_chunks(rid)
        if rec and rec.transcription:
            self.transcript_view.set_transcription(rec.transcription, chunks)
            # 通知 MainWindow:VAD 模式下,全部段落库后也可触发 LLM 自动命名
            self.transcription_saved.emit(rid, rec.transcription)
        else:
            self.transcript_view.set_loading("无转写内容(所有段失败?)")

        # 进入 VLM / 笔记流程(复用 _after_transcription_ready 的后半部分逻辑)
        if self.attachment_panel.has_attachments():
            self._launch_vlm(rid)
        else:
            self._set_action_status(
                f"全部段转写完成({self._completed_segments} 段)", loading=False
            )
            self.btn_transcribe.setEnabled(True)
            self.btn_generate_note.setEnabled(True)
            self._show_toast(f"全部段转写完成（{self._completed_segments} 段）")

    # ---- VLM ----

    def _on_vlm_done(self, att_id: int, desc: str):
        self.attachment_panel.update_description(att_id, desc)

    def _on_vlm_failed(self, att_id: int, err: str):
        self.attachment_panel.update_description(att_id, f"描述失败：{err}")

    def _on_vlm_all_done(self, rid: int):
        self._show_status("转写 + 图片描述完成，可生成融合笔记")
        self._set_action_status("", loading=False)
        self.btn_transcribe.setEnabled(True)
        self.btn_generate_note.setEnabled(True)

    # ---- 融合笔记 ----

    def _on_generate_note(self):
        rid = self._current_work_rid()
        if not rid:
            return
        try:
            if self._note_fuser is None:
                self._note_fuser = NoteFuser()
        except ValueError as e:
            self._show_status(f"无法生成笔记：{e}")
            return

        self.btn_generate_note.setEnabled(False)
        self.note_view.set_loading("生成融合笔记中…")
        self._set_action_status("生成融合笔记中…", loading=True)
        self._show_status("生成融合笔记中…")

        self._swap_singleton_worker("_note_worker", NoteFuserWorker(self._note_fuser, rid))
        self._note_worker.progress.connect(self._show_status)
        self._note_worker.finished_ok.connect(self._on_note_done)
        self._note_worker.failed.connect(self._on_note_failed)
        self._note_worker.start()

    def _on_note_done(self, rid: int, note: str):
        self.note_view.set_note(note)
        self._show_toast("融合笔记已生成", duration=2500)
        self._set_action_status("", loading=False)
        self.btn_generate_note.setEnabled(True)
        # 笔记生成完毕 → 自动切到笔记 Tab，避免用户找不到结果
        self.tabs.setCurrentIndex(self.TAB_NOTE)

    def _on_note_failed(self, rid: int, err: str):
        self.note_view.set_loading(f"生成失败：{err}")
        self._show_status(f"笔记生成失败：{err}")
        self._set_action_status("生成失败", loading=False)
        self.btn_generate_note.setEnabled(True)

    def _retire_worker(self, w) -> None:
        """把完成/被替换的 worker 移入退役列表（保持 Python 引用）。

        queued 信号槽执行时 worker 线程可能尚未退出 run()，若直接丢弃
        最后一个 Python 引用，运行中的 QThread 可能被 GC 析构触发 Qt 致命错误。
        """
        if w is None:
            return
        self._retired_workers.append(w)
        # 顺手清理已结束的退役 worker，防止列表无限增长
        self._retired_workers = [x for x in self._retired_workers if x.isRunning()]

    def _set_action_status(self, text: str, loading: bool):
        self.lbl_action_status.setText(text)
        self.spinner.setVisible(loading)

    def _update_attachment_tab_title(self) -> None:
        """同步照片附件 Tab 标题上的数量。"""
        self.tabs.setTabText(
            self.TAB_ATTACHMENTS, f"照片附件 ({self.attachment_panel.count})"
        )

    def is_busy(self) -> bool:
        """是否有录音或后台任务正在进行（供 MainWindow 切档前守卫）。"""
        if self.recording_widget.recording_id() is not None:
            return True
        if self._finalize_pending:
            return True
        for w in (self._transcription_worker, self._vlm_worker,
                  self._note_worker, self._realtime_worker):
            if w is not None and w.isRunning():
                return True
        for w in self._segment_workers:
            if w.isRunning():
                return True
        return False

    def cleanup_workers(self, timeout_ms: int = 8000):
        """窗口关闭时由 MainWindow 调用，等待各 page 的 worker 退出。

        共享 ``timeout_ms`` 总预算（网络型 worker 超过单线程 3s 是常态）；
        超时仍运行的线程 terminate 兜底——QThread 在线程运行中被析构会触发
        Qt 致命错误「Destroyed while thread is still running」（进程 abort），
        关闭场景下强停比崩溃更可取。
        """
        # 先停 VAD 订阅(防止音频线程继续触发段截断信号)
        if self._vad is not None:
            try:
                self.service.recorder.remove_subscriber(self._vad.consume_frame)
            except Exception:
                pass
            try:
                self._vad.stop()
            except Exception:
                pass
            self._vad = None

        threads = []
        for w in list(self._segment_workers) + list(self._retired_workers):
            if w is not None and w.isRunning():
                threads.append(w)
        for w in (self._transcription_worker, self._vlm_worker,
                  self._note_worker, self._realtime_worker):
            if w is None:
                continue
            # realtime worker 需先请求停止再等待（它在 _stop_event.wait 阻塞）
            if hasattr(w, "request_stop"):
                w.request_stop()
            if w.isRunning():
                threads.append(w)

        deadline = time.monotonic() + timeout_ms / 1000.0
        for w in threads:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms > 0:
                w.wait(remaining_ms)
        for w in threads:
            if w.isRunning():
                logger.warning(
                    f"worker {type(w).__name__} 未在 {timeout_ms}ms 内退出，"
                    f"terminate 兜底（窗口关闭路径）"
                )
                w.terminate()
                w.wait(1000)
        self._segment_workers = []
        self._retired_workers = []
