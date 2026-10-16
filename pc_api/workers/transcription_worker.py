"""转写后台任务（QThread）。

从录音的 audio_path 读取音频，调 core 转写连接器，结果存入 DB。
工作线程内通过 RecordingService 操作 DB（service 每方法独立 session）。
"""

import logging

from PySide6.QtCore import QThread, Signal

from core.transcription import get_connector, TranscriptionRequest
from core.transcription.exceptions import TranscriptionError, ConfigurationError
from pc_api.recording_service import RecordingService

logger = logging.getLogger(__name__)


class TranscriptionWorker(QThread):
    """转写工作线程。"""

    progress = Signal(str)                    # 进度消息
    finished_ok = Signal(int, str)            # (recording_id, 转写文本)
    failed = Signal(int, str)                 # (recording_id, 错误信息)

    def __init__(self, service: RecordingService, recording_id: int):
        super().__init__()
        self.service = service
        self.recording_id = recording_id

    def run(self) -> None:
        rid = self.recording_id
        try:
            self.progress.emit("准备音频文件…")
            self.service.set_recording_status(rid, "PROCESSING")

            with self.service.materialize_audio(rid) as materialized:
                audio_path = materialized.local_path
                self.progress.emit("调用转写引擎…")

                connector = get_connector()
                with open(audio_path, "rb") as f:
                    request = TranscriptionRequest(
                        audio_file=f,
                        filename="audio.wav",
                        mime_type="audio/wav",
                        # Pro 连接器（火山/fun-asr）需本地路径上传到托管服务
                        extra_options={"local_path": audio_path},
                    )
                    response = connector.transcribe(request)

            self.progress.emit("保存转写结果…")
            self.service.save_transcription(rid, response)
            self.finished_ok.emit(rid, response.text)

        except ConfigurationError as e:
            logger.error(f"转写配置错误: {e}")
            # 先发终态信号再落库状态：DB 写失败时 GUI 仍能收到失败通知
            self.failed.emit(rid, f"配置错误：{e}（请检查转写 API key）")
            try:
                self.service.set_recording_status(rid, "FAILED", error=str(e))
            except Exception:
                logger.exception("录音状态落库失败")
        except TranscriptionError as e:
            logger.error(f"转写失败: {e}")
            self.failed.emit(rid, f"转写失败：{e}")
            try:
                self.service.set_recording_status(rid, "FAILED", error=str(e))
            except Exception:
                logger.exception("录音状态落库失败")
        except Exception as e:
            logger.exception("转写异常")
            self.failed.emit(rid, f"未知错误：{e}")
            try:
                self.service.set_recording_status(rid, "FAILED", error=str(e))
            except Exception:
                logger.exception("录音状态落库失败")
