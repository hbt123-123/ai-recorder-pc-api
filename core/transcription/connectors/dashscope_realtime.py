"""DashScope paraformer-realtime-v2 实时 ASR 连接器(WebSocket)。

本地 wav 转 PCM 16kHz mono 后流式上行 DashScope 实时识别,返回带时间戳的
sentence。纯客户端、无需公网 URL,契合"不部署服务器、各版本独立运行"定位。

用户配置:transcription_api_key(DashScope key)、transcription_model
(默认 paraformer-realtime-v2)、language(zh/en/空)。

依赖:dashscope SDK、soundfile、numpy。
"""

import io
import logging
import threading
from typing import Any, Dict, List, Optional, Set

import numpy as np
import soundfile as sf

from ..base import (
    BaseTranscriptionConnector,
    TranscriptionCapability,
    ConnectorSpecifications,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
)
from ..exceptions import ConfigurationError, TranscriptionError

logger = logging.getLogger(__name__)

# 默认 PCM 采样率（可被 config.sample_rate 覆盖；8k-v2 模型用 8000）
_DEFAULT_TARGET_SR = 16000
# 完成等待超时:至少 30s,按音频时长 ×2 上浮
_BASE_TIMEOUT = 30.0


class _ResultCollector:
    """收集 dashscope Recognition 回调结果(线程安全)。

    on_event 在 SDK 内部线程触发;只保留 is_sentence_end 的最终句子。
    """

    def __init__(self):
        self.sentences: List[Dict[str, Any]] = []
        self.error: Optional[Exception] = None
        self.completed = threading.Event()
        self.opened = threading.Event()

    def on_open(self) -> None:
        self.opened.set()
        logger.info("dashscope_realtime: WebSocket 已连接")

    def on_event(self, result) -> None:
        try:
            from dashscope.audio.asr import RecognitionResult
            sentence = result.get_sentence()
            if not sentence:
                return
            # get_sentence 可能返回 dict 或 list(list 取末项为当前句)
            if isinstance(sentence, list):
                sentence = sentence[-1] if sentence else None
            if sentence and RecognitionResult.is_sentence_end(sentence):
                self.sentences.append(sentence)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"dashscope_realtime on_event 解析异常: {e}")

    def on_error(self, result) -> None:
        msg = getattr(result, "message", None) or str(result)
        self.error = TranscriptionError(f"DashScope 实时 ASR 错误: {msg}")
        logger.error(f"dashscope_realtime on_error: {msg}")
        self.completed.set()

    def on_complete(self) -> None:
        self.completed.set()
        logger.info("dashscope_realtime: 识别完成")

    def on_close(self) -> None:
        self.completed.set()


class DashScopeRealtimeConnector(BaseTranscriptionConnector):
    """DashScope paraformer-realtime-v2 实时 ASR 连接器。"""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
    }
    PROVIDER_NAME = "dashscope_realtime"
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,
        max_duration_seconds=None,
        handles_chunking_internally=True,
    )

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_key = config["api_key"]
        self.model = config.get("model") or "paraformer-realtime-v2"
        # 采样率由 config 决定：paraformer-realtime-v2=16000，8k-v2=8000
        self.sample_rate = int(config.get("sample_rate") or _DEFAULT_TARGET_SR)
        self.language = (config.get("language") or "").strip() or None

    def _validate_config(self) -> None:
        if not self.config.get("api_key"):
            raise ConfigurationError(
                "api_key is required for dashscope_realtime connector"
            )

    @staticmethod
    def _to_pcm_mono(audio_bytes: bytes, target_sr: int) -> tuple:
        """读音频 bytes → 重采样到 target_sr Hz mono int16 PCM bytes + 时长(秒)。"""
        data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=True)
        # always_2d=True → shape (N, channels)；无条件取第一声道 → 1D
        data = data[:, 0]
        # 重采样到 target_sr(numpy 线性插值)
        if sr != target_sr:
            n = int(round(len(data) * target_sr / sr))
            if n < 1:
                n = 1
            idx = np.linspace(0, len(data) - 1, n)
            data = np.interp(idx, np.arange(len(data)), data).astype(np.float32)
        pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        duration = len(data) / target_sr
        return pcm, duration

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        try:
            import dashscope
            from dashscope.audio.asr import Recognition

            # 1. 读音频并转 PCM mono（采样率由 config 决定）
            audio_bytes = request.audio_file.read()
            if not audio_bytes:
                raise TranscriptionError("音频为空")
            pcm, duration = self._to_pcm_mono(audio_bytes, self.sample_rate)
            logger.info(
                f"dashscope_realtime: 音频 {duration:.1f}s, {len(pcm)} bytes PCM, "
                f"model={self.model} sample_rate={self.sample_rate}"
            )

            # 2. 配置回调与识别器
            dashscope.api_key = self.api_key
            collector = _ResultCollector()
            lang = request.language or self.language
            kwargs: Dict[str, Any] = {}
            if lang:
                kwargs["language_hints"] = [lang]

            recognition = Recognition(
                model=self.model,
                callback=collector,
                format="pcm",
                sample_rate=self.sample_rate,
                **kwargs,
            )
            # start() 成功后任何失败路径都必须 stop()，否则 WebSocket 连接与
            # SDK 内部线程泄漏，批量转写遇网络抖动会逐次累积耗尽资源。
            session_started = False
            try:
                recognition.start()
                session_started = True

                # 3. 等连接建立(避免过早发帧丢失)
                if not collector.opened.wait(timeout=15.0):
                    raise TranscriptionError("DashScope WebSocket 连接超时")

                # 4. 分片发送 PCM(200ms/帧,快速发送,服务端缓冲)
                frame_bytes = int(self.sample_rate * 0.2)
                for i in range(0, len(pcm), frame_bytes):
                    recognition.send_audio_frame(pcm[i:i + frame_bytes])
                recognition.stop()
                session_started = False  # 正常收尾，finally 不再重复 stop
            finally:
                if session_started:
                    try:
                        recognition.stop()
                    except Exception as stop_err:
                        logger.warning(f"dashscope_realtime: 异常路径 stop 失败: {stop_err}")

            # 5. 等待识别完成
            timeout = max(_BASE_TIMEOUT, duration * 2.0)
            if not collector.completed.wait(timeout=timeout):
                raise TranscriptionError(
                    f"DashScope 实时 ASR 超时({timeout:.0f}s)"
                )
            if collector.error:
                raise collector.error

            # 6. 拼装结果
            segments: List[TranscriptionSegment] = []
            text_parts: List[str] = []
            for s in collector.sentences:
                t = (s.get("text") or "").strip()
                if not t:
                    continue
                begin = s.get("begin_time")
                end = s.get("end_time")
                segments.append(TranscriptionSegment(
                    text=t,
                    start_time=begin / 1000.0 if begin is not None else None,
                    end_time=end / 1000.0 if end is not None else None,
                ))
                text_parts.append(t)

            full_text = "".join(text_parts)
            logger.info(
                f"dashscope_realtime: 完成, {len(segments)} 句, {len(full_text)} 字"
            )

            return TranscriptionResponse(
                text=full_text,
                segments=segments or None,
                language=lang,
                duration=duration,
                provider=self.PROVIDER_NAME,
                model=self.model,
            )

        except TranscriptionError:
            raise
        except Exception as e:
            logger.error(f"dashscope_realtime 转写失败: {e}")
            raise TranscriptionError(f"DashScope 实时 ASR 失败: {e}") from e

    def list_models(self):
        """dashscope 无统一的 OpenAI 兼容 /models,返回已知 ASR 模型。"""
        return [
            {"id": "paraformer-realtime-8k-v2", "label": "paraformer-realtime-8k-v2（8kHz）"},
            {"id": "paraformer-realtime-v2", "label": "paraformer-realtime-v2（16kHz）"},
            {"id": "paraformer-realtime-v1", "label": "paraformer-realtime-v1"},
        ]

    def health_check(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {"type": "string", "description": "DashScope API key"},
                "model": {
                    "type": "string",
                    "default": "paraformer-realtime-v2",
                    "description": "实时 ASR 模型名（8k-v2 用 8000 采样率）",
                },
                "sample_rate": {
                    "type": "integer",
                    "default": 16000,
                    "description": "PCM 采样率：paraformer-realtime-v2=16000, 8k-v2=8000",
                },
                "language": {
                    "type": "string",
                    "description": "语言提示(zh/en),空则自动检测",
                },
            },
        }
