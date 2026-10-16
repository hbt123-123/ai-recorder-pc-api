"""分段转写后台任务(QThread)。

VAD 截断的**单段**音频 → 调对应 tier 连接器 → 落库为 TranscriptChunk
+ emit 信号供 UI 增量显示。与 ``TranscriptionWorker`` 区别:
- 输入是单段本地 audio_path(已写入 staging),不读 Recording.audio_path
- 结果**追加**(append)而非覆盖,段间 chunk_index 全局递增
- 完成后 emit ``chunk_done`` 携带新 chunks 元数据(已加段起始时间偏移)
  供 UI 调用 ``TranscriptView.append_final`` 增量显示,模拟 realtime
  ``final_sentence`` 信号体验

回退:失败 emit ``segment_failed``,不影响其他段;若全部段失败由 GUI
决定是否走整段 ``TranscriptionWorker`` 兜底。
"""

import logging

from PySide6.QtCore import QThread, Signal

from core.transcription import get_connector, TranscriptionRequest
from core.transcription.base import TranscriptionResponse
from core.transcription.exceptions import TranscriptionError, ConfigurationError
from pc_api.recording_service import RecordingService

logger = logging.getLogger(__name__)


class SegmentTranscriptionWorker(QThread):
    """单段音频转写 worker。

    信号:
        chunk_done(int, int, list): (recording_id, segment_index,
            [(text, start_s, end_s), ...]) 段内 chunk 已加段起始偏移。
        segment_failed(int, int, str): (recording_id, segment_index, err)
        status(str): 进度消息。
    """

    chunk_done = Signal(int, int, list)
    segment_failed = Signal(int, int, str)
    status = Signal(str)

    def __init__(self, service: RecordingService, recording_id: int,
                 segment_index: int, segment_id: int, audio_path: str):
        super().__init__()
        self.service = service
        self.recording_id = recording_id
        self.segment_index = segment_index
        self.segment_id = segment_id
        self.audio_path = audio_path

    def run(self) -> None:
        sid = self.segment_id
        seg_idx = self.segment_index
        try:
            self.status.emit(f"转写段 {seg_idx} 中…")
            self.service.set_segment_status(sid, "PROCESSING")

            connector = get_connector()
            with open(self.audio_path, "rb") as f:
                request = TranscriptionRequest(
                    audio_file=f,
                    filename=f"rec_seg{seg_idx}.wav",
                    mime_type="audio/wav",
                    # Pro 连接器(火山/fun-asr)需本地路径上传到托管服务
                    extra_options={"local_path": self.audio_path},
                )
                response: TranscriptionResponse = connector.transcribe(request)

            # 落库(段内 chunk_index 全局递增,时间戳加段起始偏移)
            self.service.save_segment_transcription(
                self.recording_id, sid, response
            )

            # 取段起始时间,给 UI 显示用的 chunk 时间戳加偏移
            seg_start = self.service.get_segment_start_time(sid) or 0.0
            chunks_meta = []
            if response.segments:
                for s in response.segments:
                    if not s.text or not s.text.strip():
                        continue
                    chunks_meta.append((
                        s.text,
                        (s.start_time + seg_start) if s.start_time is not None else None,
                        (s.end_time + seg_start) if s.end_time is not None else None,
                    ))
            elif response.text and response.text.strip():
                chunks_meta.append((response.text, seg_start, None))

            self.status.emit(
                f"段 {seg_idx} 转写完成,共 {len(chunks_meta)} 句"
            )
            self.chunk_done.emit(self.recording_id, seg_idx, chunks_meta)

        except (ConfigurationError, TranscriptionError) as e:
            logger.error(f"段 {seg_idx} 转写失败: {e}")
            # 先发终态信号再落库状态：DB 写失败时 GUI 仍能收到失败通知
            self.segment_failed.emit(self.recording_id, seg_idx, str(e))
            try:
                self.service.set_segment_status(sid, "FAILED", error=str(e))
            except Exception:
                logger.exception(f"段 {seg_idx} 状态落库失败")
        except Exception as e:
            logger.exception(f"段 {seg_idx} 转写异常")
            self.segment_failed.emit(self.recording_id, seg_idx, str(e))
            try:
                self.service.set_segment_status(sid, "FAILED", error=str(e))
            except Exception:
                logger.exception(f"段 {seg_idx} 状态落库失败")
