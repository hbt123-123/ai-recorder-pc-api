"""历史会话详情视图(只读)。

右侧主区在选中历史会话时显示:
  QFrame#SessionDetailView
  ├─ QFrame#detailHeader
  │  ├─ QLabel title (可双击改名)
  │  ├─ QLabel meta (时间/时长/会议模式/tier)
  │  └─ 操作按钮行:重新转写 / 重新生成笔记 / 打开音频文件夹 / 删除
  └─ QTabWidget
     ├─ 转写文本 (TranscriptView,只读)
     ├─ 融合笔记 (NoteView)
     └─ 照片附件 (AttachmentPanel,只读)

注意:本视图自建独立的 TranscriptView / NoteView / AttachmentPanel 实例,
不与实时工作区 BaseTierPage 共享,保证"对话记录分离"。

信号:
- session_renamed(int, str):  标题更新 → 通知 MainWindow 刷新侧边栏
- session_deleted(int):       删除会话 → 通知 MainWindow 切回实时工作区
- retrain_requested(int):     重新转写 → 通知 MainWindow 启动独立 worker
- regen_note_requested(int):  重新生成笔记 → 通知 MainWindow 启动 NoteFuserWorker
"""

import logging
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QInputDialog, QMessageBox,
)
from PySide6.QtCore import QUrl

from core.models import Recording
from pc_api.recording_service import RecordingService
from pc_api.gui.transcript_view import TranscriptView
from pc_api.gui.note_view import NoteView
from pc_api.gui.attachment_panel import AttachmentPanel
from pc_api.gui.audio_player import AudioPlayer
from pc_api.gui.styles import Metrics
from pc_api.gui.icons import make_icon

logger = logging.getLogger(__name__)


def _format_meta(rec: Recording, tier_label: str = "") -> str:
    """meta 行:时间 / 时长 / 会议 / tier。"""
    parts = []
    if rec.created_at:
        parts.append(rec.created_at.strftime("%Y-%m-%d %H:%M"))
    if rec.audio_duration_seconds:
        parts.append(f"{rec.audio_duration_seconds:.0f} 秒")
    if getattr(rec, "meeting_mode", False):
        parts.append("会议模式")
    if tier_label:
        parts.append(tier_label)
    if rec.completed_at:
        parts.append("已完成")
    return "  ·  ".join(parts)


def _format_status(status: str) -> str:
    """中文状态显示。"""
    return {
        "COMPLETED": "已完成",
        "PROCESSING": "处理中",
        "FAILED": "失败",
        "PENDING": "待处理",
    }.get(status, status or "未知")


class SessionDetailView(QFrame):
    """历史会话详情只读视图。"""

    session_renamed = Signal(int, str)
    session_deleted = Signal(int)
    retrain_requested = Signal(int)
    regen_note_requested = Signal(int)

    def __init__(self, service: RecordingService, parent=None):
        super().__init__(parent)
        self.setObjectName("SessionDetailView")
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._service = service
        self._current_rid: int = 0

        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(Metrics.SPACING_MD)
        outer.setContentsMargins(
            Metrics.SPACING_LG, Metrics.SPACING_LG,
            Metrics.SPACING_LG, Metrics.SPACING_LG,
        )

        # ---- 头部 ----
        header = QFrame()
        header.setObjectName("detailHeader")
        h_layout = QVBoxLayout(header)
        h_layout.setSpacing(Metrics.SPACING_SM)
        h_layout.setContentsMargins(
            Metrics.SPACING_LG, Metrics.SPACING_MD,
            Metrics.SPACING_LG, Metrics.SPACING_MD,
        )

        # 标题行
        title_row = QHBoxLayout()
        title_row.setSpacing(Metrics.SPACING_SM)
        self.title_lbl = QLabel("（未选择会话）")
        self.title_lbl.setObjectName("detailTitle")
        self.title_lbl.setWordWrap(True)
        title_row.addWidget(self.title_lbl, 1)

        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("detailStatus")
        self.status_lbl.setAlignment(Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(self.status_lbl, 0)
        h_layout.addLayout(title_row)

        # meta 行
        self.meta_lbl = QLabel("")
        self.meta_lbl.setObjectName("detailMeta")
        h_layout.addWidget(self.meta_lbl)

        # 操作按钮行
        btn_row = QHBoxLayout()
        btn_row.setSpacing(Metrics.SPACING_SM)

        self.btn_retrain = QPushButton("重新转写")
        self.btn_retrain.setObjectName("toolButton")
        self.btn_retrain.setIcon(make_icon("refresh", 14))
        self.btn_retrain.setToolTip("对该录音重新跑一遍转写(覆盖现有转写)")
        self.btn_retrain.clicked.connect(self._on_retrain_clicked)

        self.btn_regen_note = QPushButton("重新生成笔记")
        self.btn_regen_note.setObjectName("toolButton")
        self.btn_regen_note.setIcon(make_icon("note", 14))
        self.btn_regen_note.setToolTip("重新调 LLM 融合笔记")
        self.btn_regen_note.clicked.connect(self._on_regen_note_clicked)

        self.btn_open_folder = QPushButton("打开音频文件夹")
        self.btn_open_folder.setObjectName("toolButton")
        self.btn_open_folder.setIcon(make_icon("folder", 14))
        self.btn_open_folder.setToolTip("在系统文件管理器中打开音频所在文件夹")
        self.btn_open_folder.clicked.connect(self._on_open_folder_clicked)

        self.btn_delete = QPushButton("删除")
        self.btn_delete.setObjectName("toolButton")
        self.btn_delete.setIcon(make_icon("trash", 14))
        self.btn_delete.setToolTip("删除该会话(不可撤销)")
        self.btn_delete.clicked.connect(self._on_delete_clicked)

        btn_row.addWidget(self.btn_retrain)
        btn_row.addWidget(self.btn_regen_note)
        btn_row.addWidget(self.btn_open_folder)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_delete)
        h_layout.addLayout(btn_row)

        outer.addWidget(header)

        # ---- 极简音频播放器（回放 + 转写点击跳转） ----
        self.player = AudioPlayer()
        outer.addWidget(self.player)

        # ---- 工作区 Tab(三个 view 各构造独立实例,不与 BaseTierPage 共享) ----
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)

        # 重要:独立构造,避免与实时工作区 BaseTierPage 的实例串扰
        self.transcript_view = TranscriptView()
        self.tabs.addTab(self.transcript_view, "转写文本")

        self.note_view = NoteView()
        self.tabs.addTab(self.note_view, "融合笔记")

        self.attachment_panel = AttachmentPanel()
        self.tabs.addTab(self.attachment_panel, "照片附件 (0)")

        outer.addWidget(self.tabs, 1)

        # 播放器 ↔ 转写联动：播放进度高亮气泡；点气泡跳转播放位置
        self.player.position_changed.connect(self.transcript_view.set_highlight)
        self.transcript_view.chunk_clicked.connect(self.player.seek_to)

        # 初始未加载,所有操作按钮禁用
        self._set_actions_enabled(False)

    def _set_actions_enabled(self, enabled: bool) -> None:
        self.btn_retrain.setEnabled(enabled)
        self.btn_regen_note.setEnabled(enabled)
        self.btn_open_folder.setEnabled(enabled)
        self.btn_delete.setEnabled(enabled)

    # ---- 公开 API ----

    def load_recording(self, rid: int) -> None:
        """加载某个历史录音到详情视图。"""
        rec = self._service.get_recording(rid)
        if rec is None:
            self.clear()
            return
        self._current_rid = rid
        self._render_recording(rec)

    def refresh_from_db(self) -> None:
        """从 DB 重新加载当前显示的会话(用于重新转写 / 重新生成笔记后刷新)。"""
        if self._current_rid > 0:
            self.load_recording(self._current_rid)

    def clear(self) -> None:
        """清空视图(切回实时工作区时调用)。"""
        self._current_rid = 0
        self.title_lbl.setText("（未选择会话）")
        # 状态属性设在 status_lbl 上(与 _render_recording 一致),清除后需刷新样式
        self.status_lbl.setText("")
        self.status_lbl.setProperty("status", None)
        self.status_lbl.style().unpolish(self.status_lbl)
        self.status_lbl.style().polish(self.status_lbl)
        self.meta_lbl.setText("")
        self.transcript_view.clear()
        self.note_view.clear()
        self.attachment_panel.clear()
        self.player.stop()
        self._set_actions_enabled(False)
        self._update_tab_title(0)

    def current_rid(self) -> int:
        """返回当前显示的 rid(0 表示未加载)。"""
        return self._current_rid

    # ---- 内部 ----

    def _render_recording(self, rec: Recording) -> None:
        """把 Recording 对象渲染到 UI。"""
        self.title_lbl.setText(rec.title or f"录音 #{rec.id}")
        self.meta_lbl.setText(_format_meta(rec))
        self.status_lbl.setText(_format_status(rec.status))
        self.status_lbl.setProperty("status", rec.status or "PENDING")
        self.status_lbl.style().unpolish(self.status_lbl)
        self.status_lbl.style().polish(self.status_lbl)

        # 转写 + chunks
        chunks = self._service.list_chunks(rec.id)
        self.transcript_view.set_transcription(
            rec.transcription or "", chunks
        )

        # 笔记
        self.note_view.set_note(rec.summary or "")

        # 附件
        self.attachment_panel.clear()
        for a in self._service.list_attachments(rec.id):
            try:
                img_path = self._service.resolve_local_path(a.storage_locator)
                source = getattr(a, "source", None) or "camera"
                self.attachment_panel.add_attachment(
                    a.id, img_path, a.timestamp, a.description or "",
                    source=source,
                )
            except Exception as e:
                logger.warning(f"加载附件失败 att={a.id}: {e}")
        self._update_tab_title(self.attachment_panel.count)

        # 音频回放：加载到播放器（文件缺失则停播）
        try:
            audio_path = None
            if getattr(rec, "audio_path", None):
                audio_path = self._service.resolve_local_path(rec.audio_path)
            if audio_path and os.path.exists(audio_path):
                self.player.load_audio(audio_path)
            else:
                self.player.stop()
        except Exception as e:
            logger.warning(f"加载音频到播放器失败: {e}")
            self.player.stop()

        # 双击标题可重命名
        self.title_lbl.mouseDoubleClickEvent = self._on_title_double_clicked

        self._set_actions_enabled(True)

    def _update_tab_title(self, count: int) -> None:
        self.tabs.setTabText(2, f"照片附件 ({count})")

    def _on_title_double_clicked(self, _event) -> None:
        """双击标题 → 弹输入框改名。"""
        if self._current_rid <= 0:
            return
        rec = self._service.get_recording(self._current_rid)
        if rec is None:
            return
        new_title, ok = QInputDialog.getText(
            self, "重命名会话", "新标题:", text=rec.title or "",
        )
        if not ok:
            return
        self._service.rename_recording(self._current_rid, new_title)
        # 重查 DB 拿最新
        again = self._service.get_recording(self._current_rid)
        if again is not None:
            self._render_recording(again)
        # 通知上层(刷新侧边栏)
        self.session_renamed.emit(self._current_rid, new_title or "")

    def _on_retrain_clicked(self) -> None:
        if self._current_rid <= 0:
            return
        self.retrain_requested.emit(self._current_rid)

    def _on_regen_note_clicked(self) -> None:
        if self._current_rid <= 0:
            return
        self.regen_note_requested.emit(self._current_rid)

    def _on_open_folder_clicked(self) -> None:
        """打开音频文件所在文件夹(用系统文件管理器)。"""
        if self._current_rid <= 0:
            return
        rec = self._service.get_recording(self._current_rid)
        if rec is None or not rec.audio_path:
            QMessageBox.information(self, "提示", "该会话没有可定位的音频文件")
            return
        try:
            file_path = self._service.resolve_local_path(rec.audio_path)
            if not file_path or not os.path.exists(file_path):
                QMessageBox.information(self, "提示", f"音频文件已不存在:\n{file_path}")
                return
            # 用系统文件管理器打开音频所在文件夹（并非定位/选中文件本身）
            folder = os.path.dirname(file_path)
            url = QUrl.fromLocalFile(folder)
            QDesktopServices.openUrl(url)
        except Exception as e:
            logger.exception("打开音频文件夹失败")
            QMessageBox.warning(self, "打开失败", str(e))

    def _on_delete_clicked(self) -> None:
        if self._current_rid <= 0:
            return
        rec = self._service.get_recording(self._current_rid)
        title = (rec.title if rec and rec.title else f"会话 #{self._current_rid}")
        ok = QMessageBox.question(
            self, "删除会话",
            f"确认删除「{title}」?\n该操作不可撤销,音频/转写/笔记/附件将一并删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ok != QMessageBox.StandardButton.Yes:
            return
        try:
            self._service.delete_recording(self._current_rid)
        except Exception as e:
            logger.exception("删除录音失败")
            QMessageBox.critical(self, "删除失败", str(e))
            return
        rid = self._current_rid
        self.clear()
        self.session_deleted.emit(rid)
