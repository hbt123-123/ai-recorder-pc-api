"""文件上传转写页面（顶栏「文件转写」分段对应）。

独立于 4 个 tier 录音页：用户上传本地音频文件 → 选择四种转写方式之一
→ VAD 分段逐段转写（与实时录音一致）→ 增量显示 → 生成融合笔记。

页面结构：
  - 头部卡片：标题 + 说明
  - 转写方式选择行：4 档 tier（QComboBox，切换时注入对应 env + 重建连接器）
  - 操作行：选择音频文件 / 开始转写 / 生成融合笔记 + spinner + 状态
  - 工作区 Tab：转写 / 笔记

与 MainWindow 对接的信号（复用录音页桥接）：
  - transcription_saved(int, str)：转写落库 → 触发 LLM 自动命名
  - recording_session(int, bool)：会话创建/结束 → 侧边栏刷新
"""

import logging
import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTabWidget,
    QLabel, QSizePolicy, QFrame, QFileDialog, QComboBox,
)

from core.fusion import NoteFuser
from core.transcription.tiers import list_tiers, get_tier
from core.user_config import UserConfig
from pc_api.recording_service import RecordingService
from pc_api.gui.transcript_view import TranscriptView
from pc_api.gui.note_view import NoteView
from pc_api.gui.styles import Spinner, Metrics, get_palette
from pc_api.gui.icons import make_icon, icon_pixmap
from pc_api.workers import FileTranscriptionWorker, NoteFuserWorker
from pc_api.config_loader import apply_tier_env

logger = logging.getLogger(__name__)

# 上传音频大小上限（与火山豆包连接器 512MB 上限对齐）
_MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_ALLOWED_AUDIO_EXTS = {".wav", ".flac", ".ogg", ".mp3", ".m4a"}


class FileUploadPage(QWidget):
    """音频文件上传转写页面（四种转写方式可选）。"""

    # 转写已落库 → MainWindow 启动 AutoTitleWorker（参数: rid, text）
    transcription_saved = Signal(int, str)
    # 会话创建/结束 → MainWindow 刷新侧边栏（参数: rid, is_active）
    recording_session = Signal(int, bool)

    TAB_TRANSCRIPT = 0
    TAB_NOTE = 1

    def __init__(self, service: RecordingService, cfg: UserConfig,
                 parent=None):
        super().__init__(parent)
        self.service = service
        self.cfg = cfg
        self._file_worker: FileTranscriptionWorker = None
        self._note_worker: NoteFuserWorker = None
        # 已完成但线程可能尚未退出的 worker（保持引用防 QThread 运行中被析构）
        self._retired_workers = []
        self._note_fuser = None  # 延迟构造（需 API key）
        self._upload_rid = None  # 当前上传文件对应的 recording_id
        self._file_path = ""     # 当前选中的本地音频文件路径

        self._build_ui()
        self._connect()

    # ---- UI ----

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(Metrics.SPACING_MD)
        outer.setContentsMargins(
            Metrics.SPACING_LG, Metrics.SPACING_LG,
            Metrics.SPACING_LG, Metrics.SPACING_MD
        )

        # 1. 头部卡片
        header = QFrame()
        header.setObjectName("Card")
        header.setFrameShape(QFrame.Shape.NoFrame)
        h_layout = QVBoxLayout(header)
        h_layout.setSpacing(Metrics.SPACING_SM)
        h_layout.setContentsMargins(
            Metrics.SPACING_LG, Metrics.SPACING_MD,
            Metrics.SPACING_LG, Metrics.SPACING_MD
        )

        title_row = QHBoxLayout()
        title_row.setSpacing(Metrics.SPACING_SM)
        icon_lbl = QLabel()
        icon_lbl.setPixmap(icon_pixmap("file", 20, get_palette().PRIMARY))
        title_row.addWidget(icon_lbl)
        title = QLabel("音频文件转写")
        title.setObjectName("titleLabel")
        title_row.addWidget(title)
        title_row.addStretch()
        h_layout.addLayout(title_row)

        subtitle = QLabel(
            "上传本地音频文件（WAV / FLAC / OGG / MP3 / M4A 等），"
            "选择转写方式后自动分段转写。批处理档（免费 / Lite / Pro）启用 VAD "
            "说话停顿分段，实时版走整文件转写。"
        )
        subtitle.setObjectName("subtitleLabel")
        subtitle.setWordWrap(True)
        h_layout.addWidget(subtitle)
        outer.addWidget(header)

        # 2. 转写方式选择行
        tier_row = QHBoxLayout()
        tier_row.setSpacing(Metrics.SPACING_SM)
        lbl_tier = QLabel("转写方式")
        lbl_tier.setObjectName("subtitleLabel")
        tier_row.addWidget(lbl_tier)

        self.combo_tier = QComboBox()
        for t in list_tiers():
            self.combo_tier.addItem(f"{t.label} - {t.model}", t.tier_id)
        # 默认与全局 cfg.asr_tier 一致
        self._sync_tier_combo()
        self.combo_tier.currentIndexChanged.connect(self._on_tier_changed)
        tier_row.addWidget(self.combo_tier)
        tier_row.addStretch()
        outer.addLayout(tier_row)

        # 3. 操作行：选文件 / 开始转写 / 生成笔记 + spinner + 状态
        action_bar = QHBoxLayout()
        action_bar.setSpacing(Metrics.SPACING_SM)

        self.btn_pick = QPushButton("选择音频文件")
        self.btn_pick.setObjectName("primaryButton")
        self.btn_pick.setIcon(make_icon("folder", 18, get_palette().TEXT_ON_PRIMARY))

        self.btn_transcribe = QPushButton("开始转写")
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

        self.lbl_status = QLabel("请选择要转写的音频文件")
        self.lbl_status.setObjectName("subtitleLabel")
        self.lbl_status.setMaximumWidth(400)

        action_bar.addWidget(self.btn_pick)
        action_bar.addWidget(self.btn_transcribe)
        action_bar.addWidget(self.btn_generate_note)
        action_bar.addStretch()
        action_bar.addSpacing(Metrics.SPACING_MD)
        action_bar.addWidget(self.spinner)
        action_bar.addWidget(self.lbl_status)
        outer.addLayout(action_bar)

        # 4. 工作区 Tab：转写 / 笔记
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)

        self.transcript_view = TranscriptView()
        self.tabs.addTab(self.transcript_view, "转写文本")

        self.note_view = NoteView()
        self.tabs.addTab(self.note_view, "融合笔记")

        outer.addWidget(self.tabs, 1)

    def _sync_tier_combo(self):
        """把 combo 选中态同步到全局 cfg.asr_tier。"""
        tier_id = getattr(self.cfg, "asr_tier", "free")
        for i in range(self.combo_tier.count()):
            if self.combo_tier.itemData(i) == tier_id:
                self.combo_tier.setCurrentIndex(i)
                return
        self.combo_tier.setCurrentIndex(0)

    def _on_tier_changed(self):
        """切换转写方式：注入对应 env + 重建连接器。

        注意：env 与连接器 registry 是进程级全局状态，此处的切换会影响
        其他页面后续调 get_connector() 的结果（如详情页"重新转写"）。
        录音页的 cfg.asr_tier 字段保持不变，切回录音页并点选顶栏时
        MainWindow 会重新 apply_config 校正。
        """
        tier_id = self.combo_tier.currentData()
        if not tier_id:
            return
        try:
            apply_tier_env(tier_id, getattr(self.cfg, "language", "") or "")
            from core.transcription.registry import get_registry
            get_registry().reinitialize()
            t = get_tier(tier_id)
            self.lbl_status.setText(f"已切换转写方式：{t.label} - {t.price}")
        except Exception as e:
            logger.warning(f"切换转写方式失败 {tier_id}: {e}")
            self.lbl_status.setText(f"切换转写方式失败：{e}")

    def _connect(self):
        self.btn_pick.clicked.connect(self._on_pick_file)
        self.btn_transcribe.clicked.connect(self._on_start_transcribe)
        self.btn_generate_note.clicked.connect(self._on_generate_note)

    # ---- 文件选择 ----

    def _on_pick_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择音频文件", "",
            "音频文件 (*.wav *.flac *.ogg *.mp3 *.m4a);;所有文件 (*.*)"
        )
        if not file_path:
            return
        # 若已有进行中的转写，忽略
        if self._file_worker is not None and self._file_worker.isRunning():
            self.lbl_status.setText("上一个文件正在转写中，请稍候…")
            return

        err = self._validate_audio_file(file_path)
        if err:
            self.lbl_status.setText(err)
            return

        self._file_path = file_path
        filename = os.path.basename(file_path)
        self.lbl_status.setText(f"已选择：{filename}，点击「开始转写」")
        self.btn_transcribe.setEnabled(True)
        self.btn_generate_note.setEnabled(False)
        self.transcript_view.clear()
        self.note_view.clear()

    @staticmethod
    def _validate_audio_file(path: str) -> str:
        """校验所选文件：存在 / 扩展名 / 大小 / 可解码。返回错误文案，None 通过。

        避免任意大文件被整份拷入 storage 后才由解码器报错，
        留下 FAILED 记录与磁盘垃圾。
        注意白名单中的 .mp3/.m4a 依赖 libsndfile 构建选项（m4a/AAC 通常
        不支持），故额外用 sf.info 试探性打开，提前给出友好报错。
        """
        if not os.path.exists(path):
            return f"文件不存在：{path}"
        ext = os.path.splitext(path)[1].lower()
        if ext not in _ALLOWED_AUDIO_EXTS:
            allowed = " ".join(sorted(_ALLOWED_AUDIO_EXTS))
            return f"不支持的音频格式 {ext or '(无扩展名)'}（支持：{allowed}）"
        try:
            size = os.path.getsize(path)
        except OSError as e:
            return f"无法读取文件信息：{e}"
        if size == 0:
            return "文件为空"
        if size > _MAX_UPLOAD_BYTES:
            return (f"文件过大（{size / 1024 / 1024:.0f} MB），"
                    f"上限 {_MAX_UPLOAD_BYTES // 1024 // 1024} MB")
        # 解码能力探测：libsndfile 不支持的容器（如 m4a/AAC、部分 mp3）
        # 在此拦截，而不是拷贝入库后由 worker 抛英文原始错误
        try:
            import soundfile as sf
            sf.info(path)
        except ImportError:
            pass  # 未装 soundfile 时交给 worker 报错
        except Exception as e:
            return (f"无法解码该音频文件（{ext} 可能不受支持）：{e}\n"
                    f"建议转换为 WAV 后重试")
        return None

    def _on_start_transcribe(self):
        if not self._file_path:
            return
        tier_id = self.combo_tier.currentData()

        err = self._validate_audio_file(self._file_path)
        if err:
            self.lbl_status.setText(err)
            return

        # 创建录音记录（轻量 DB 操作）；原文件拷贝进 storage 由 worker 执行，
        # 避免大文件 shutil.copy2 在 GUI 主线程阻塞事件循环
        filename = os.path.basename(self._file_path)
        rec = self.service.create_recording(title=f"上传: {filename}")
        rid = rec.id

        self._upload_rid = rid
        self.btn_pick.setEnabled(False)
        self.btn_transcribe.setEnabled(False)
        self.btn_generate_note.setEnabled(False)
        self._set_status("文件转写中…", loading=True)
        self.tabs.setCurrentIndex(self.TAB_TRANSCRIPT)

        # 通知 MainWindow：新会话创建 → 侧边栏添加 + 标红点
        self.recording_session.emit(rid, True)

        # 启动文件转写 worker（tier_id 由页面内选择决定）
        self._file_worker = FileTranscriptionWorker(
            self.service, rid, self._file_path, self.cfg, tier_id
        )
        self._file_worker.progress.connect(self._set_status)
        self._file_worker.segment_done.connect(self._on_segment_done)
        self._file_worker.all_done.connect(self._on_transcribe_done)
        self._file_worker.failed.connect(self._on_transcribe_failed)
        self._file_worker.start()

    def _on_segment_done(self, rid: int, seg_idx: int, chunks_meta: list) -> None:
        """单段转写完成：增量显示。"""
        for text, start, end in chunks_meta:
            if not text or not text.strip():
                continue
            self.transcript_view.append_final(text, start, end)
        self.lbl_status.setText(f"段 {seg_idx} 转写完成…")

    def _on_transcribe_done(self, rid: int, text: str) -> None:
        """全部完成：重渲染 + 触发自动命名 + 启用笔记按钮。"""
        self._retire_worker(self._file_worker)
        self._file_worker = None
        self.btn_pick.setEnabled(True)
        self.btn_transcribe.setEnabled(False)

        rec = self.service.get_recording(rid)
        chunks = self.service.list_chunks(rid)
        self.transcript_view.set_transcription(
            rec.transcription if rec and rec.transcription else text, chunks
        )

        saved_text = rec.transcription if rec and rec.transcription else text
        self.transcription_saved.emit(rid, saved_text)
        # 会话结束 → 侧边栏清红点 + 刷新
        self.recording_session.emit(rid, False)

        self._set_status("文件转写完成，可生成融合笔记", loading=False)
        self.btn_generate_note.setEnabled(True)

    def _on_transcribe_failed(self, rid: int, err: str) -> None:
        self._retire_worker(self._file_worker)
        self._file_worker = None
        self.btn_pick.setEnabled(True)
        self.btn_transcribe.setEnabled(False)
        self.transcript_view.set_loading(f"文件转写失败：{err}")
        self._set_status(f"文件转写失败：{err}", loading=False)
        self.recording_session.emit(rid, False)

    # ---- 融合笔记 ----

    def _on_generate_note(self):
        rid = self._upload_rid
        if not rid:
            return
        try:
            if self._note_fuser is None:
                self._note_fuser = NoteFuser()
        except ValueError as e:
            self.lbl_status.setText(f"无法生成笔记：{e}")
            return

        self.btn_generate_note.setEnabled(False)
        self.note_view.set_loading("生成融合笔记中…")
        self._set_status("生成融合笔记中…", loading=True)

        self._note_worker = NoteFuserWorker(self._note_fuser, rid)
        self._note_worker.progress.connect(self._set_status)
        self._note_worker.finished_ok.connect(self._on_note_done)
        self._note_worker.failed.connect(self._on_note_failed)
        self._note_worker.start()

    def _on_note_done(self, rid: int, note: str):
        self._retire_worker(self._note_worker)
        self._note_worker = None
        self.note_view.set_note(note)
        self._set_status("融合笔记已生成", loading=False)
        self.btn_generate_note.setEnabled(True)
        self.tabs.setCurrentIndex(self.TAB_NOTE)

    def _on_note_failed(self, rid: int, err: str):
        self._retire_worker(self._note_worker)
        self._note_worker = None
        self.note_view.set_loading(f"生成失败：{err}")
        self._set_status(f"笔记生成失败：{err}", loading=False)
        self.btn_generate_note.setEnabled(True)

    # ---- 状态 ----

    def _retire_worker(self, w) -> None:
        """把完成回调到达的 worker 移入退役列表。

        queued 信号槽执行时 worker 线程可能尚未退出 run()，若直接丢弃
        最后一个 Python 引用，运行中的 QThread 可能被 GC 析构（Qt 致命错误）。
        """
        if w is None:
            return
        self._retired_workers.append(w)
        # 顺手清理已结束的退役 worker
        self._retired_workers = [x for x in self._retired_workers if x.isRunning()]

    def is_busy(self) -> bool:
        """是否有后台任务进行中（供 MainWindow 设置/切档守卫）。"""
        for w in (self._file_worker, self._note_worker):
            if w is not None and w.isRunning():
                return True
        return False

    def _set_status(self, text: str, loading: bool = False):
        self.lbl_status.setText(text)
        self.spinner.setVisible(loading)

    def cleanup_workers(self, timeout_ms: int = 6000):
        """窗口关闭时等待 worker 退出（共享预算 + 超时 terminate 兜底）。"""
        import time as _t
        threads = []
        for w in (self._file_worker, self._note_worker):
            if w is None:
                continue
            if hasattr(w, "request_stop"):
                try:
                    w.request_stop()
                except Exception:
                    pass
            if w.isRunning():
                threads.append(w)
        for w in list(self._retired_workers):
            if w.isRunning():
                threads.append(w)
        deadline = _t.monotonic() + timeout_ms / 1000.0
        for w in threads:
            remaining_ms = int((deadline - _t.monotonic()) * 1000)
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
        self._file_worker = None
        self._note_worker = None
        self._retired_workers = []
