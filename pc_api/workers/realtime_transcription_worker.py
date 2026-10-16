"""实时流式转写后台任务（QThread）。

录音期间订阅 AudioRecorder 实时音频帧，喂给 DashScopeLiveSession，把 SDK
partial/final 回调转为 Qt 信号回 GUI 主线程。停止后落库，VLM/笔记流程
照常可用。

线程模型：
- run()/start()/stop() 在本 worker 线程（QThread）。
- _on_audio_frame 在 sounddevice 音频线程调用：仅做轻量转发。
- _WorkerCallback 的 on_* 方法在 dashscope SDK 内部工作线程调用：仅做
  信号发射 + 列表 append，不阻塞。

安全保证：
- self._sentences 由 SDK 线程写、worker 线程在 session.stop() 返回后读
  （SDK 已 join，无并发）。
- 所有信号在 run() 返回前发射，QThread 存活。
- session.stop() 阻塞 join SDK 线程，只在 worker 线程调用，不卡 UI。
"""

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QThread, Signal

from core.audio.recorder import AudioRecorder
from core.transcription.base import (
    TranscriptionResponse,
    TranscriptionSegment,
)
from core.transcription.connectors.dashscope_realtime_stream import (
    DashScopeLiveSession,
    LiveASRCallback,
)
from pc_api.recording_service import RecordingService

logger = logging.getLogger(__name__)


class _WorkerCallback(LiveASRCallback):
    """SDK 线程回调：把事件转为 Qt 信号 emit。

    持有 worker 引用，所有方法仅做信号发射 + 列表 append，不阻塞。
    """

    def __init__(self, worker: "RealtimeTranscriptionWorker"):
        self._worker = worker

    def on_open(self) -> None:
        # 连接已建立，GUI 已在 start 前显示"连接中"，无需额外通知
        pass

    def on_partial(self, text: str) -> None:
        # 跨线程信号：Qt 自动 QueuedConnection 回 GUI 主线程
        self._worker.partial_text.emit(text)

    def on_final(self, text: str, begin_ms: float, end_ms: float) -> None:
        # 收集最终句，供停止后拼装 TranscriptionResponse（SDK 线程写，
        # worker 线程在 session.stop() 返回后读，已 join 无并发）
        self._worker._sentences.append((text, begin_ms, end_ms))
        self._worker.final_sentence.emit(text, begin_ms / 1000.0, end_ms / 1000.0)

    def on_error(self, message: str) -> None:
        self._worker._aborted = True
        self._worker.failed.emit(f"实时转写失败：{message}")
        # 自愈：直接唤醒 stop 阶段，不再依赖 GUI 收到 failed 后的 request_stop
        # 时序（GUI 侧 request_stop 幂等，重复 set 无副作用）
        self._worker._stop_event.set()

    def on_complete(self) -> None:
        pass

    def on_close(self) -> None:
        pass


class RealtimeTranscriptionWorker(QThread):
    """实时流式转写工作线程。

    信号：
        partial_text(str)：当前生长句中间结果（GUI 灰斜体）。
        final_sentence(str, float, float)：最终句（text, begin_s, end_s）。
        status(str)：进度/状态消息。
        failed(str)：失败消息（实时流异常；录音不受影响）。
        finished_ok(int, str)：停止后落库成功（recording_id, 全文）。
    """

    partial_text = Signal(str)
    final_sentence = Signal(str, float, float)
    status = Signal(str)
    failed = Signal(str)
    finished_ok = Signal(int, str)
    # 停止阶段收尾完成的终态信号（无论成功/失败/中断都必发）。
    # GUI 停止录音后进入等待收尾状态，依赖此信号退出等待并恢复按钮，
    # 仅靠 finished_ok/failed 无法覆盖 aborted 与停止阶段异常路径。
    finalize_done = Signal(int)

    def __init__(self, service: RecordingService, recording_id: int,
                 recorder: AudioRecorder, config: Dict[str, Any]):
        super().__init__()
        self.service = service
        self.recording_id = recording_id
        self.recorder = recorder
        self.config = config
        # 状态
        self._stop_event = threading.Event()
        self._stopping = False           # 音频帧回调看到后立即停止投递
        self._stop_phase_started = False # 停止阶段是否已开始（finalize_done 依据）
        self._aborted = False            # SDK 报错，停止后不落库
        self._sentences: List[Tuple[str, float, float]] = []
        self._session: Optional[DashScopeLiveSession] = None
        self._audio_frame_count = 0      # 收到的音频帧数（首帧打一次 INFO）

    def run(self) -> None:
        rid = self.recording_id
        try:
            self.status.emit("实时转写连接中…")
            self._session = DashScopeLiveSession(
                self.config, _WorkerCallback(self)
            )
            self._session.start()
            self.status.emit("实时转写已连接，请开始说话…")
            # 订阅音频帧（音频线程回调 _on_audio_frame）
            self.recorder.add_subscriber(self._on_audio_frame)

            # 阻塞直到 GUI 调 request_stop() 或 SDK 报错（on_error 置 _aborted
            # 但不主动 set _stop_event，由 GUI 收到 failed 信号后调用 request_stop）
            self._stop_event.wait()

            # ---- 停止阶段 ----
            self._stop_phase_started = True
            # 先置 _stopping，防止音频回调在 remove 后仍向已停 Recognition 投帧
            self._stopping = True
            self.recorder.remove_subscriber(self._on_audio_frame)
            self.status.emit("正在整理实时转写…")

            # 阻塞 join SDK 线程，所有回调处理完后返回
            self._session.stop()

            # 若 SDK 报错，不落库，由 GUI 走文件转写回退
            if self._aborted:
                return

            # 拼装结果并落库
            segments = [
                TranscriptionSegment(
                    text=t,
                    start_time=begin / 1000.0,
                    end_time=end / 1000.0,
                )
                for t, begin, end in self._sentences
                if t and t.strip()
            ]
            full_text = "".join(s.text for s in segments)

            if segments:
                response = TranscriptionResponse(
                    text=full_text,
                    segments=segments,
                    provider="dashscope_realtime_stream",
                    model=self.config.get("model", ""),
                )
                self.service.save_transcription(rid, response)
            else:
                # 无最终句：标记完成但无转写内容
                self.service.set_recording_status(rid, "COMPLETED")

            self.status.emit(f"实时转写完成，共 {len(segments)} 句")
            self.finished_ok.emit(rid, full_text)

        except Exception as e:
            logger.exception("realtime worker 异常")
            self.failed.emit(f"实时转写异常：{e}")
        finally:
            # 兜底清理订阅者，防止泄漏（即使异常路径）
            try:
                self.recorder.remove_subscriber(self._on_audio_frame)
            except Exception:
                pass
            # 停止阶段已开始则必发终态信号：aborted / 停止阶段异常路径
            # 原本只发 failed 或什么都不发，GUI 等待收尾的状态会永久挂起
            if self._stop_phase_started:
                self.finalize_done.emit(rid)

    def _on_audio_frame(self, indata, frames: int) -> None:
        """音频线程回调：把帧喂给实时会话。

        _stopping=True 后立即停止投帧，避免向已停 Recognition 投帧。
        单帧异常不影响录音主路径。
        """
        if self._stopping:
            return
        session = self._session
        if session is None:
            return
        self._audio_frame_count += 1
        if self._audio_frame_count == 1:
            logger.info(
                f"realtime worker: 首个音频帧到达 frames={frames} "
                f"dtype={indata.dtype} shape={indata.shape}"
            )
        try:
            session.send_audio_frame(indata)
        except Exception:
            logger.debug("实时音频帧投递失败", exc_info=True)

    def request_stop(self) -> None:
        """GUI 线程调用：请求停止（非阻塞）。

        worker 线程的 run() 会在 _stop_event.wait() 返回后进入停止阶段。
        """
        self._stop_event.set()
