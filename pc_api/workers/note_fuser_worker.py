"""融合笔记生成后台任务（QThread）。

调 core.fusion.NoteFuser 生成图文融合笔记，避免阻塞 UI。
"""

import logging

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)


class NoteFuserWorker(QThread):
    """融合笔记生成工作线程。"""

    progress = Signal(str)                  # 进度消息
    finished_ok = Signal(int, str)          # (recording_id, 笔记 Markdown)
    failed = Signal(int, str)               # (recording_id, 错误)

    def __init__(self, fuser, recording_id: int):
        super().__init__()
        self.fuser = fuser
        self.recording_id = recording_id

    def run(self) -> None:
        rid = self.recording_id
        try:
            self.progress.emit("生成融合笔记中…")
            note = self.fuser.generate_note(rid)
            self.finished_ok.emit(rid, note)
        except Exception as e:
            logger.error(f"融合笔记生成失败: {e}")
            self.failed.emit(rid, str(e))
