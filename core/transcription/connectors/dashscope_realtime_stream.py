"""DashScope 实时流式 ASR 会话封装。

与文件式连接器（dashscope_realtime.py）解耦：本模块仅负责"实时会话"的
start / send_audio_frame / stop 生命周期，把 SDK 回调转换为 partial/final
语义供 RealtimeTranscriptionWorker 消费。

设计要点：
- 麦克风固定 16kHz int16（AudioRecorder 标准格式），目标采样率由 config
  决定（paraformer-realtime-8k-v2=8000，paraformer-realtime-v2=16000）。
- send_audio_frame 在音频线程调用：仅做 numpy 重采样 + SDK 内部 Queue.put，
  微秒级、非阻塞。
- stop() 阻塞 join SDK 工作线程，**只在 worker QThread 内调用**，不得在
  GUI 线程调用，否则会卡 UI。
- _send_lock + _running 双重判定，杜绝向已停 Recognition 投帧触发
  InvalidParameter。
"""

import logging
import threading
from typing import Any, Dict

import numpy as np

logger = logging.getLogger(__name__)


class LiveASRCallback:
    """实时 ASR 回调接口（用户实现，跑在 SDK 线程）。

    所有方法在 dashscope Recognition 的内部工作线程触发；实现方应仅做
    轻量动作（如 Qt 信号 emit），不得阻塞或抛异常。
    """

    def on_open(self) -> None:
        """WebSocket 已连接。"""

    def on_partial(self, text: str) -> None:
        """当前生长句的中间结果（is_sentence_end=False）。"""

    def on_final(self, text: str, begin_ms: float, end_ms: float) -> None:
        """最终句落地（is_sentence_end=True）。时间戳单位毫秒。"""

    def on_error(self, message: str) -> None:
        """SDK 错误。"""

    def on_complete(self) -> None:
        """识别完成（服务端主动结束）。"""

    def on_close(self) -> None:
        """WebSocket 已关闭。"""


class _LiveCollector:
    """SDK 回调适配器：把 dashscope Recognition 事件转为 LiveASRCallback。

    实例跑在 SDK 内部工作线程；on_event 解析 sentence，区分 partial/final。
    """

    def __init__(self, callback: LiveASRCallback):
        self._callback = callback
        self.opened = threading.Event()

    def on_open(self) -> None:
        self.opened.set()
        logger.info("dashscope_realtime_stream: WebSocket 已连接")
        try:
            self._callback.on_open()
        except Exception:
            logger.exception("LiveASRCallback.on_open 异常")

    def on_event(self, result) -> None:
        try:
            from dashscope.audio.asr import RecognitionResult
            sentence = result.get_sentence()
            if not sentence:
                return
            # get_sentence 可能返回 dict 或 list（list 取末项为当前句）
            if isinstance(sentence, list):
                sentence = sentence[-1] if sentence else None
                if sentence is None:
                    return
            text = (sentence.get("text") or "").strip()
            is_end = RecognitionResult.is_sentence_end(sentence)
            if is_end:
                # 最终句：跳过空文本
                if not text:
                    return
                begin = sentence.get("begin_time")
                end = sentence.get("end_time")
                logger.info(f"dashscope_realtime_stream: final 「{text}」")
                self._callback.on_final(
                    text,
                    float(begin) if begin is not None else 0.0,
                    float(end) if end is not None else 0.0,
                )
            else:
                # partial：当前生长句中间结果
                if text:
                    self._callback.on_partial(text)
        except Exception:
            logger.exception("dashscope_realtime_stream on_event 解析异常")

    def on_error(self, result) -> None:
        msg = getattr(result, "message", None) or str(result)
        logger.error(f"dashscope_realtime_stream on_error: {msg}")
        try:
            self._callback.on_error(msg)
        except Exception:
            logger.exception("LiveASRCallback.on_error 异常")

    def on_complete(self) -> None:
        logger.info("dashscope_realtime_stream: 识别完成")
        try:
            self._callback.on_complete()
        except Exception:
            logger.exception("LiveASRCallback.on_complete 异常")

    def on_close(self) -> None:
        logger.info("dashscope_realtime_stream: WebSocket 已关闭")
        try:
            self._callback.on_close()
        except Exception:
            logger.exception("LiveASRCallback.on_close 异常")


class DashScopeLiveSession:
    """DashScope 实时 ASR 会话封装。

    生命周期：start() → （循环 send_audio_frame）→ stop()。
    线程模型：start/stop 在 worker QThread；send_audio_frame 在音频线程。
    """

    # 麦克风固定 16kHz（AudioRecorder.SAMPLE_RATE）
    INPUT_SAMPLE_RATE = 16000

    def __init__(self, config: Dict[str, Any], callback: LiveASRCallback):
        if not config.get("api_key"):
            raise ValueError("api_key is required for DashScopeLiveSession")
        self.api_key = config["api_key"]
        self.model = config.get("model") or "paraformer-realtime-v2"
        self.sample_rate = int(config.get("sample_rate") or self.INPUT_SAMPLE_RATE)
        self.language = (config.get("language") or "").strip() or None
        self._callback = callback
        self._collector = _LiveCollector(callback)
        self._recognition = None
        self._running = False
        self._first_frame_sent = False  # 首帧投递成功后置 True（仅打一次 INFO）
        self._send_lock = threading.Lock()
        self._start_lock = threading.Lock()

    def start(self) -> None:
        """启动实时识别会话（同步触发 on_open，WebSocket 握手在 SDK 内部异步）。"""
        import dashscope
        from dashscope.audio.asr import Recognition

        with self._start_lock:
            if self._running:
                return
            dashscope.api_key = self.api_key
            kwargs: Dict[str, Any] = {}
            if self.language:
                kwargs["language_hints"] = [self.language]
            self._recognition = Recognition(
                model=self.model,
                callback=self._collector,
                format="pcm",
                sample_rate=self.sample_rate,
                **kwargs,
            )
            self._recognition.start()
            with self._send_lock:
                self._running = True
        logger.info(
            f"dashscope_realtime_stream: 已启动 model={self.model} "
            f"sample_rate={self.sample_rate} lang={self.language or 'auto'}"
        )

    def send_audio_frame(self, indata) -> None:
        """发送一帧麦克风音频（int16, shape (frames,1), 16kHz）。

        在音频线程调用。SDK 的 send_audio_frame 内部仅 Queue.put，非阻塞。
        """
        if not self._running or self._recognition is None:
            return
        pcm = self._resample_to_target(indata)
        with self._send_lock:
            if not self._running or self._recognition is None:
                return
            try:
                self._recognition.send_audio_frame(pcm.tobytes())
                if not self._first_frame_sent:
                    self._first_frame_sent = True
                    logger.info(
                        f"dashscope_realtime_stream: 首帧已投递 "
                        f"frames={len(pcm)} bytes={pcm.nbytes} "
                        f"sample_rate={self.sample_rate}"
                    )
            except Exception as e:
                # 投帧失败不得影响录音主路径，但需可见以便诊断
                logger.warning(f"send_audio_frame 投帧失败：{e}")

    def stop(self) -> None:
        """停止会话（阻塞 join SDK 工作线程，所有回调处理完后返回）。

        必须在 worker QThread 内调用，不得在 GUI 线程调用。
        """
        with self._start_lock:
            with self._send_lock:
                self._running = False
            rec = self._recognition
            self._recognition = None
        if rec is not None:
            try:
                rec.stop()
            except Exception:
                logger.debug("recognition.stop 异常", exc_info=True)

    def _resample_to_target(self, indata) -> np.ndarray:
        """麦克风 16kHz int16 → 目标采样率 int16 ndarray。

        与 dashscope_realtime._to_pcm_mono 同算法（np.linspace + np.interp）。
        每帧独立重采样，帧边界处有 1 样本误差（~每 64ms 一次），对 ASR 无影响。

        注意：data 是 int16 数值域 [-32768, 32767]，np.interp 返回值同样落在该
        域内；直接量化回 int16 即可。切勿误用归一化 [-1,1] 的 clip×scale，否则
        正常语音会被削成满量程方波，模型无法识别。
        """
        # indata: int16, shape (frames, 1) 或 (frames,)
        samples = indata[:, 0] if indata.ndim > 1 else indata.ravel()
        if len(samples) == 0:
            # 空帧：直接返回空数组，避免下方 linspace(0,-1)/np.interp 空样本点抛错
            return np.zeros((0,), dtype=np.int16)
        if self.sample_rate == self.INPUT_SAMPLE_RATE:
            return np.ascontiguousarray(samples.astype(np.int16, copy=False))
        # float32 线性插值重采样（数值范围保持 int16 域 [-32768, 32767]）
        data = samples.astype(np.float32)
        n = int(round(len(data) * self.sample_rate / self.INPUT_SAMPLE_RATE))
        if n < 1:
            n = 1
        idx = np.linspace(0, len(data) - 1, n)
        resampled = np.interp(idx, np.arange(len(data)), data)
        # resampled 值域与输入一致；clip 仅防插值端点浮点越界
        return np.clip(resampled, -32768.0, 32767.0).astype(np.int16)
