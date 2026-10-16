"""LLM 自动生成会话标题的后台 Worker(QThread)。

触发时机:实时录音工作区录音停止 + 转写完成(save_transcription 落库后)。
不阻塞 UI,完成后发 finished_ok 信号,MainWindow 据此刷新侧边栏。

设计要点:
- 复用 NoteFuser 已构造的 client(OpenAI 兼容),不重复初始化
- 失败 fallback:不调用 service.rename_recording,保持原默认标题
- 短文本(< 20 字)直接 fallback,避免 LLM 给噪音生成奇怪标题
"""

import logging

from PySide6.QtCore import QThread, Signal

from core.fusion.note_fuser import NoteFuser

logger = logging.getLogger(__name__)


class AutoTitleWorker(QThread):
    """LLM 自动生成会话标题 worker。"""

    progress = Signal(str)                # 进度消息
    finished_ok = Signal(int, str)        # (recording_id, new_title) 成功
    failed = Signal(int, str)             # (recording_id, error) 失败

    def __init__(self, fuser: NoteFuser, recording_id: int,
                 transcript: str, parent=None):
        super().__init__(parent)
        self.fuser = fuser
        self.recording_id = recording_id
        self.transcript = transcript or ""

    def run(self) -> None:
        rid = self.recording_id
        try:
            self.progress.emit("正在生成会话标题…")
            new_title = self.fuser.generate_title(self.transcript)
            if not new_title:
                # fallback:保留原标题,signal 不发 finished_ok
                logger.info(
                    f"AutoTitleWorker: rid={rid} 未生成新标题(短文本/无 client/失败)"
                )
                self.failed.emit(rid, "未生成新标题(短文本或无 LLM client)")
                return
            logger.info(f"AutoTitleWorker: rid={rid} 生成标题 -> {new_title!r}")
            self.finished_ok.emit(rid, new_title)
        except Exception as e:
            logger.exception(f"AutoTitleWorker 异常: rid={rid}")
            self.failed.emit(rid, str(e))
