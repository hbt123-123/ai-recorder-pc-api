"""左侧对话历史侧边栏(仿 ChatGPT / Claude 风格)。

UI 结构:
  QFrame#SessionHistorySidebar
  ├─ QPushButton("开启新对话", primaryButton)
  ├─ QLineEdit(placeholder="搜索会话…")  [250ms 防抖]
  └─ QScrollArea
     └─ QWidget 容器
        ├─ 分组 1: section header + list items
        ├─ 分组 2: ...
        └─ 空状态提示

每条记录项 _SessionItemWidget:
  ├─ 标题(粗体, recording.title 或 fallback)
  ├─ 副标题: HH:MM · 124s
  └─ 状态徽标: 🟢/🟡/🔴/⚪ + (录音中) 🔴 小红点

信号:
  - session_selected(int):  点击历史项,携带 rid
  - new_session_requested(): 点击「开启新对话」

外部调用:
  - refresh(): 重新从 DB 加载全部
  - add_recording(recording): 新录音创建后插入
  - update_recording_title(rid, new_title): 标题更新(LLM 自动命名后)
  - remove_recording(rid): 删除后移除
  - set_active(rid): 程序化高亮当前项
  - set_recording_rid(rid): 当前正在录音的 rid(显示小红点)
"""

import logging
from datetime import date
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QAction
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit,
    QLabel, QListWidget, QListWidgetItem, QWidget,
    QSizePolicy, QMenu, QInputDialog, QMessageBox, QApplication,
)

from core.models import Recording
from pc_api.recording_service import RecordingService
from pc_api.gui.styles import Metrics, get_palette
from pc_api.gui.icons import make_icon

logger = logging.getLogger(__name__)


# ============================================================
# 工具函数:时间分组(可单测)
# ============================================================

def _group_recordings(records: List[Recording]) -> List[Tuple[str, List[Recording]]]:
    """按时间分组:今天 → 7天内 → 30天内 → 按月份降序。

    边界规则(与 ChatGPT 风格一致):
    - 今天: 同一日期
    - 7 天内: 0 < 间隔 < 7 天(不含今天,不含第 7 天)
    - 30 天内: 7 ≤ 间隔 < 30 天
    - 月份组: 30 天及更早,按 YYYY-MM 聚合,降序排列

    返回: [(section_label, [rec, ...]), ...] 已按显示顺序排好
    """
    today = date.today()
    grp_today: List[Recording] = []
    grp_7d: List[Recording] = []
    grp_30d: List[Recording] = []
    grp_monthly: dict[str, List[Recording]] = {}

    for r in records:
        created = r.created_at
        if created is None:
            # 无 created_at 的脏数据兜底放月份组(用 id 字符串保持稳定)
            grp_monthly.setdefault("未知", []).append(r)
            continue
        d = created.date()
        delta_days = (today - d).days
        if delta_days == 0:
            grp_today.append(r)
        elif 0 < delta_days < 7:
            grp_7d.append(r)
        elif 7 <= delta_days < 30:
            grp_30d.append(r)
        else:
            key = d.strftime("%Y-%m")
            grp_monthly.setdefault(key, []).append(r)

    result: List[Tuple[str, List[Recording]]] = []
    if grp_today:
        result.append(("今天", grp_today))
    if grp_7d:
        result.append(("7 天内", grp_7d))
    if grp_30d:
        result.append(("30 天内", grp_30d))
    # 月份降序(YYYY-MM 字符串可直接字典序)
    for k in sorted(grp_monthly.keys(), reverse=True):
        result.append((k, grp_monthly[k]))
    return result


# ============================================================
# 单条记录项
# ============================================================

def _status_badge(status: str) -> str:
    """状态 -> 纯文本中文字。"""
    return {
        "COMPLETED": "已完成",
        "PROCESSING": "处理中",
        "FAILED": "失败",
        "PENDING": "待处理",
    }.get(status, "待处理")


def _format_meta(rec: Recording) -> str:
    """副标题: HH:MM · 124s · (会议)"""
    parts = []
    if rec.created_at:
        parts.append(rec.created_at.strftime("%m-%d %H:%M"))
    if rec.audio_duration_seconds:
        parts.append(f"{rec.audio_duration_seconds:.0f}s")
    if getattr(rec, "meeting_mode", False):
        parts.append("会议")
    return " · ".join(parts) if parts else ""


def _format_title(rec: Recording) -> str:
    """主标题:title 或 fallback。"""
    if rec.title and rec.title.strip():
        return rec.title
    if rec.created_at:
        return f"录音 {rec.created_at.strftime('%Y-%m-%d %H:%M')}"
    return f"录音 #{rec.id}"


class _SessionItemWidget(QWidget):
    """侧边栏单条记录项:标题 + 副标题 + 状态徽标 + 录音中红点。"""

    def __init__(self, rec: Recording, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(10, 6, 10, 6)

        # 标题行 + 状态徽标
        title_row = QHBoxLayout()
        title_row.setSpacing(4)
        self.title_lbl = QLabel(_format_title(rec))
        self.title_lbl.setObjectName("sessionItemTitle")
        self.title_lbl.setWordWrap(False)
        self.title_lbl.setTextFormat(Qt.TextFormat.PlainText)
        # 截断省略号
        fm = self.title_lbl.fontMetrics()
        self.title_lbl.setText(
            fm.elidedText(_format_title(rec), Qt.TextElideMode.ElideRight, 220)
        )
        title_row.addWidget(self.title_lbl, 1)

        self.status_lbl = QLabel(_status_badge(rec.status or "PENDING"))
        self.status_lbl.setObjectName("sessionItemStatus")
        self.status_lbl.setProperty("status", rec.status or "PENDING")
        self.status_lbl.setTextFormat(Qt.TextFormat.PlainText)
        title_row.addWidget(self.status_lbl, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(title_row)

        # 副标题行 + 录音中红点
        meta_row = QHBoxLayout()
        meta_row.setSpacing(4)
        self.meta_lbl = QLabel(_format_meta(rec))
        self.meta_lbl.setObjectName("sessionItemMeta")
        meta_row.addWidget(self.meta_lbl, 1)

        self.recording_lbl = QLabel("")  # 录音中标记
        self.recording_lbl.setObjectName("sessionItemRecording")
        meta_row.addWidget(self.recording_lbl, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(meta_row)

        # 保存 rec id 供查找
        self.recording_id = rec.id

    def set_recording(self, is_recording: bool) -> None:
        """录音中显示标记。"""
        self.recording_lbl.setText("录音中" if is_recording else "")

    def update_recording(self, rec: Recording) -> None:
        """刷新单条记录的标题/状态显示(LLM 改名后或 DB 更新后)。"""
        fm = self.title_lbl.fontMetrics()
        self.title_lbl.setText(
            fm.elidedText(_format_title(rec), Qt.TextElideMode.ElideRight, 220)
        )
        self.status_lbl.setText(_status_badge(rec.status or "PENDING"))
        self.status_lbl.setProperty("status", rec.status or "PENDING")
        # 重新 polish 触发 [status="..."] 属性样式
        self.status_lbl.style().unpolish(self.status_lbl)
        self.status_lbl.style().polish(self.status_lbl)
        self.meta_lbl.setText(_format_meta(rec))


# ============================================================
# 侧边栏主类
# ============================================================

class SessionHistorySidebar(QFrame):
    """左侧历史会话侧边栏(仿 ChatGPT)。"""

    session_selected = Signal(int)         # 携带 rid
    recording_updated = Signal(int, str)   # 就地改名完成 (rid, 新标题)；不切换主区视图
    new_session_requested = Signal()       # 「开启新对话」点击

    def __init__(self, service: RecordingService, parent=None):
        super().__init__(parent)
        self.setObjectName("SessionHistorySidebar")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumWidth(220)
        self.setMaximumWidth(320)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        self._service = service
        self._current_rid: Optional[int] = None  # 当前选中(主区显示)rid
        self._recording_rid: Optional[int] = None  # 当前正在录音的 rid
        # 缓存所有 Recording 引用,避免 _SessionItemWidget 持有过期对象
        self._records_by_id: dict[int, Recording] = {}
        # 防抖定时器
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(250)
        self._search_debounce.timeout.connect(self._do_search)

        self._build_ui()
        self._install_keyboard_shortcuts()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(Metrics.SPACING_SM)
        layout.setContentsMargins(
            Metrics.SPACING_MD, Metrics.SPACING_LG,
            Metrics.SPACING_SM, Metrics.SPACING_LG,
        )

        # 「开启新对话」按钮
        self.btn_new = QPushButton("  开启新对话")
        self.btn_new.setObjectName("primaryButton")
        p = get_palette()
        self.btn_new.setIcon(make_icon("plus", 18, p.TEXT_ON_PRIMARY))
        self.btn_new.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new.clicked.connect(self.new_session_requested.emit)
        layout.addWidget(self.btn_new)

        # 搜索框
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("sessionSearch")
        self.search_edit.setPlaceholderText("搜索会话…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        layout.addWidget(self.search_edit)

        # 滚动列表(用 QListWidget 按 rid 索引 item)
        self.list_widget = QListWidget()
        self.list_widget.setObjectName("SessionHistoryList")
        self.list_widget.setFrameShape(QFrame.Shape.NoFrame)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list_widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.list_widget.setSpacing(2)
        # 安装事件过滤器:右键菜单 + 上下键导航
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._on_context_menu)
        self.list_widget.currentRowChanged.connect(self._on_row_changed)
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.list_widget, 1)

        # 空状态提示(初始隐藏)
        self._empty_hint = QLabel("暂无历史会话\n开始一段新录音吧")
        self._empty_hint.setObjectName("sessionEmptyHint")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint.setWordWrap(True)
        self._empty_hint.hide()
        # 空状态必须加入布局，否则 show() 不会出现在界面上。
        # 列表仍保留在上方，用户可随时使用「开启新对话」和搜索框。
        layout.addWidget(self._empty_hint)

    def _install_keyboard_shortcuts(self):
        """↑↓ 键切换选中项。"""
        QShortcut(QKeySequence(Qt.Key.Key_Down), self.list_widget,
                  activated=self._select_next)
        QShortcut(QKeySequence(Qt.Key.Key_Up), self.list_widget,
                  activated=self._select_prev)

    def _select_next(self):
        """↓ 键:跳到下一条;若已在末尾则不操作。"""
        row = self.list_widget.currentRow()
        n = self.list_widget.count()
        if n == 0:
            return
        if row < 0:
            self.list_widget.setCurrentRow(0)
        elif row < n - 1:
            self.list_widget.setCurrentRow(row + 1)

    def _select_prev(self):
        """↑ 键:跳到上一条。"""
        row = self.list_widget.currentRow()
        if row > 0:
            self.list_widget.setCurrentRow(row - 1)

    # ---- 公开 API ----

    def refresh(self) -> None:
        """从 DB 重新加载全部会话(分组 + 渲染)。"""
        keyword = self.search_edit.text().strip()
        try:
            if keyword:
                records = self._service.search_recordings(keyword)
            else:
                records = self._service.list_recordings()
        except Exception:
            logger.exception("刷新历史会话失败")
            records = []

        self._records_by_id = {r.id: r for r in records}
        self._rebuild_list(records)

    def add_recording(self, rec: Recording) -> None:
        """新录音创建后插入列表顶部(避免全量 refresh 闪烁)。

        不立即 addItem —— 真实场景下录音刚刚 create,还没转写完成;
        留作上层 MainWindow 显式调用(完整刷新可重排分组)。
        """
        # 简单实现:直接 refresh;后续可优化为 insertItem 头部
        self.refresh()
        self.set_active(rec.id)

    def update_recording_title(self, rid: int, new_title: str) -> None:
        """更新单条标题(LLM 自动命名后)。

        当前实现:重查 DB 单条记录 → 调 _SessionItemWidget.update_recording;
        简单可靠,避免构造 Recording 对象的字段不一致问题。
        """
        rec = self._service.get_recording(rid)
        if rec is None:
            return
        self._records_by_id[rid] = rec
        # 找到对应 item 并刷新
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item is None:
                continue
            w = self.list_widget.itemWidget(item)
            if isinstance(w, _SessionItemWidget) and w.recording_id == rid:
                w.update_recording(rec)
                return

    def remove_recording(self, rid: int) -> None:
        """删除某条记录(从列表移除)。"""
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item is None:
                continue
            w = self.list_widget.itemWidget(item)
            if isinstance(w, _SessionItemWidget) and w.recording_id == rid:
                # 屏蔽信号再移除：takeItem 会令 Qt 把当前行跳到相邻条目，
                # 触发 currentRowChanged → session_selected，主区意外跳转
                self.list_widget.blockSignals(True)
                try:
                    self.list_widget.takeItem(i)
                finally:
                    self.list_widget.blockSignals(False)
                break
        self._records_by_id.pop(rid, None)
        if self._current_rid == rid:
            self._current_rid = None
            # 仅删除当前选中项时才清高亮；删其他项不应抹掉现有选择
            self.list_widget.setCurrentRow(-1)
        # 清理变空的分组标题行
        self._prune_empty_group_headers()
        # 空状态判断按记录项计数（分组标题 UserRole=-1 不计入）
        if not self._has_record_items():
            self._empty_hint.show()

    def _prune_empty_group_headers(self) -> None:
        """删除其后已无成员记录项的分组标题行。"""
        i = 0
        while i < self.list_widget.count():
            item = self.list_widget.item(i)
            if item is None:
                continue
            rid = item.data(Qt.ItemDataRole.UserRole)
            if rid == -1:
                nxt = (self.list_widget.item(i + 1)
                       if i + 1 < self.list_widget.count() else None)
                nxt_rid = (nxt.data(Qt.ItemDataRole.UserRole)
                           if nxt is not None else None)
                if nxt is None or nxt_rid == -1:
                    self.list_widget.blockSignals(True)
                    try:
                        self.list_widget.takeItem(i)
                    finally:
                        self.list_widget.blockSignals(False)
                    continue
            i += 1

    def _has_record_items(self) -> bool:
        """列表中是否仍有记录项（排除分组标题）。"""
        for j in range(self.list_widget.count()):
            it = self.list_widget.item(j)
            if it is None:
                continue
            r = it.data(Qt.ItemDataRole.UserRole)
            if r is not None and r != -1:
                return True
        return False

    def set_active(self, rid: Optional[int]) -> None:
        """程序化高亮当前选中项(不触发 session_selected 信号)。"""
        self._current_rid = rid
        if rid is None:
            self.list_widget.blockSignals(True)
            self.list_widget.setCurrentRow(-1)
            self.list_widget.blockSignals(False)
            return
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item is None:
                continue
            w = self.list_widget.itemWidget(item)
            if isinstance(w, _SessionItemWidget) and w.recording_id == rid:
                self.list_widget.blockSignals(True)
                self.list_widget.setCurrentRow(i)
                self.list_widget.blockSignals(False)
                return

    def set_recording_rid(self, rid: Optional[int]) -> None:
        """标记当前正在录音的 rid(显示小红点);状态变化时刷一次。"""
        old = self._recording_rid
        self._recording_rid = rid
        # 找旧/新的 item widget 刷新小红点
        for target_rid in (old, rid):
            if target_rid is None:
                continue
            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                if item is None:
                    continue
                w = self.list_widget.itemWidget(item)
                if isinstance(w, _SessionItemWidget) and w.recording_id == target_rid:
                    w.set_recording(target_rid == self._recording_rid)

    def shutdown(self) -> None:
        """预留:目前无线程/资源,空实现。"""
        pass

    # ---- 内部 ----

    def _rebuild_list(self, records: List[Recording]) -> None:
        """按分组重渲染整个列表。"""
        self.list_widget.blockSignals(True)
        try:
            self.list_widget.clear()
            if not records:
                self._empty_hint.show()
                return
            self._empty_hint.hide()
            groups = _group_recordings(records)
            for section_label, group in groups:
                # 分组标题(item 不可选,isSelectable=False 不让点击)
                header_item = QListWidgetItem(section_label)
                header_item.setFlags(Qt.ItemFlag.NoItemFlags)
                header_item.setData(Qt.ItemDataRole.UserRole, -1)  # -1 标识非记录
                header_item.setSizeHint(header_item.sizeHint().expandedTo(
                    header_item.sizeHint().__class__(0, 22)
                ))
                # 用一个 QLabel 作为 header widget(无背景,带 padding)
                header_lbl = QLabel(section_label)
                header_lbl.setObjectName("sessionSectionHeader")
                header_lbl.setContentsMargins(0, 4, 0, 4)
                self.list_widget.addItem(header_item)
                self.list_widget.setItemWidget(header_item, header_lbl)
                # 组内记录项
                for rec in group:
                    item = QListWidgetItem()
                    item.setData(Qt.ItemDataRole.UserRole, rec.id)
                    w = _SessionItemWidget(rec)
                    w.set_recording(rec.id == self._recording_rid)
                    item.setSizeHint(w.sizeHint())
                    self.list_widget.addItem(item)
                    self.list_widget.setItemWidget(item, w)
        finally:
            self.list_widget.blockSignals(False)
        # 恢复当前选中
        if self._current_rid is not None:
            self.set_active(self._current_rid)

    def _on_row_changed(self, row: int) -> None:
        if row < 0:
            return
        item = self.list_widget.item(row)
        if item is None:
            return
        rid = item.data(Qt.ItemDataRole.UserRole)
        if rid is None or rid == -1:
            # header item,跳过(实际已 NoItemFlags,row 不会选中)
            return
        if rid == self._current_rid:
            return  # 已是当前项,不重复 emit
        self._current_rid = rid
        self.session_selected.emit(rid)

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        """双击触发重命名(快速操作)。"""
        if item is None:
            return
        rid = item.data(Qt.ItemDataRole.UserRole)
        if rid is None or rid == -1:
            return
        self._rename_recording(rid)

    def _on_context_menu(self, pos) -> None:
        """右键菜单:重命名 / 复制转写 / 删除。"""
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        rid = item.data(Qt.ItemDataRole.UserRole)
        if rid is None or rid == -1:
            return
        rec = self._records_by_id.get(rid)
        if rec is None:
            return
        menu = QMenu(self.list_widget)
        act_rename = QAction("  重命名", self)
        act_rename.setIcon(make_icon("edit", 16))
        act_rename.triggered.connect(lambda: self._rename_recording(rid))
        menu.addAction(act_rename)
        # 复制转写(锦上添花)
        act_copy = QAction("  复制转写文本", self)
        act_copy.setIcon(make_icon("copy", 16))
        act_copy.triggered.connect(lambda: self._copy_transcription(rec))
        menu.addAction(act_copy)
        menu.addSeparator()
        act_delete = QAction("  删除", self)
        act_delete.setIcon(make_icon("trash", 16))
        act_delete.triggered.connect(lambda: self._delete_recording(rid))
        menu.addAction(act_delete)
        menu.exec(self.list_widget.mapToGlobal(pos))

    def _rename_recording(self, rid: int) -> None:
        """弹输入框 → 调 service.rename_recording → 刷新单条。"""
        rec = self._records_by_id.get(rid)
        if rec is None:
            return
        new_title, ok = QInputDialog.getText(
            self, "重命名会话", "新标题:",
            text=rec.title or "",
        )
        if not ok:
            return
        # service 内已处理空字符串保留原标题
        self._service.rename_recording(rid, new_title)
        # 重新查 DB 拿到最新对象并刷新单条显示
        self.update_recording_title(rid, new_title)
        # 用专用更新信号通知 MainWindow（详情视图若显示该 rid 也刷新）。
        # 不复用 session_selected：其语义是"选中并切换主区"，就地改名
        # 会把用户强制跳转到该会话详情页。
        self.recording_updated.emit(rid, new_title)

    def _copy_transcription(self, rec: Recording) -> None:
        """复制转写文本到剪贴板(若有)。"""
        text = rec.transcription or ""
        if not text:
            QMessageBox.information(self, "提示", "该会话暂无转写文本")
            return
        clipboard = QApplication.clipboard()
        clipboard.setText(text)
        self.status_message_for("已复制转写文本到剪贴板")

    def _delete_recording(self, rid: int) -> None:
        """二次确认 → service.delete_recording → 移除列表项。"""
        rec = self._records_by_id.get(rid)
        title = (rec.title if rec and rec.title else f"会话 #{rid}")
        ok = QMessageBox.question(
            self, "删除会话",
            f"确认删除「{title}」?\n该操作不可撤销,音频/转写/笔记/附件将一并删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ok != QMessageBox.StandardButton.Yes:
            return
        try:
            self._service.delete_recording(rid)
        except Exception as e:
            logger.exception(f"删除录音失败 rid={rid}")
            QMessageBox.critical(self, "删除失败", str(e))
            return
        # remove_recording 会清空 _current_rid，因此需在移除前保存状态；
        # 否则删除详情页当前会话后不会发出返回工作区的信号。
        was_current = self._current_rid == rid
        # 从列表移除
        self.remove_recording(rid)
        # 若删的是当前主区显示的会话,通知 MainWindow 切回实时工作区
        if was_current:
            self.new_session_requested.emit()

    def _on_search_text_changed(self, _text: str) -> None:
        """输入框变化 → 250ms 防抖 → 触发搜索。"""
        self._search_debounce.start()

    def _do_search(self) -> None:
        """防抖到期,执行搜索/全量刷新。"""
        self.refresh()

    def status_message_for(self, msg: str) -> None:
        """小提示:在状态栏显示;通过 window().statusBar() 转发。"""
        win = self.window()
        if win is not None and hasattr(win, "statusBar"):
            try:
                win.statusBar().showMessage(msg, 3000)
            except Exception:
                pass
