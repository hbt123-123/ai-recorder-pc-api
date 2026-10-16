"""阿里云百炼 fun-asr-flash-2026-06-15 录音文件识别连接器（专业版 Pro）。

基于 DashScope 同步多模态生成接口（multimodal-generation 端点）：
https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide

关键特性：
- 同步调用（非异步 submit+poll），一次请求即返回结果
- 音频以 base64 data URI 内联（无需上传 OSS / 公网 URL）
- 返回句子级 + 词级时间戳
- 限制：单个音频 ≤ 5 分钟（超长音频需在上层分段或改用 fun-asr 异步接口）
- 上下文增强：可在 messages 中前置 system 文本提升专有名词准确率

调用方式：
    POST https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
    Headers: Authorization / Content-Type / X-DashScope-SSE: disable
    Body:
      {
        "model": "fun-asr-flash-2026-06-15",
        "input": {
          "messages": [{
            "role": "user",
            "content": [{"type": "input_audio",
                         "input_audio": {"data": "data:audio/wav;base64,..."}}]
          }]
        },
        "parameters": {"format": "wav", "sample_rate": 16000}
      }
    Response: output.output.sentence.text + output.output.sentence.words[]

价格：0.792 元/小时。
"""

import base64
import io
import logging
from typing import Any, Dict, List, Optional, Set

import httpx
import numpy as np
import soundfile as sf

from ..base import (
    BaseTranscriptionConnector,
    ConnectorSpecifications,
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
)
from ..exceptions import ConfigurationError, TranscriptionError

logger = logging.getLogger(__name__)

_GEN_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"

# fun-asr-flash 同步接口限制：单音频 ≤ 5 分钟
_MAX_DURATION_SECONDS = 300
# 默认目标采样率（fun-asr-flash 推荐 16kHz）
_TARGET_SR = 16000
# HTTP 超时：5 分钟音频 + 网络余量
_HTTP_TIMEOUT = 180.0


class DashScopeFunASRConnector(BaseTranscriptionConnector):
    """阿里百炼 fun-asr-flash-2026-06-15 同步录音文件识别连接器。"""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
        TranscriptionCapability.INITIAL_PROMPT,  # 上下文增强（system 消息）
    }
    PROVIDER_NAME = "dashscope_funasr"
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=None,
        max_duration_seconds=_MAX_DURATION_SECONDS,
        handles_chunking_internally=False,  # 超过 5 分钟需上层分段
    )

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_key = config["api_key"]
        self.model = config.get("model") or "fun-asr-flash-2026-06-15"
        self.gen_url = config.get("gen_url", _GEN_URL)
        self.sample_rate = int(config.get("sample_rate") or _TARGET_SR)

    def _validate_config(self) -> None:
        if not self.config.get("api_key"):
            raise ConfigurationError(
                "api_key is required for dashscope_funasr connector"
            )

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-SSE": "disable",
        }

    @staticmethod
    def _to_wav_mono_16k(audio_bytes: bytes, target_sr: int) -> tuple:
        """读音频 bytes → 重采样到 target_sr Hz mono → WAV/PCM_16 bytes + 时长(秒)。

        与 dashscope_realtime._to_pcm_mono 同源：always_2d=True 后无条件取第一声道。
        """
        data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=True)
        data = data[:, 0]  # 无条件取第一声道 → 1D
        if sr != target_sr:
            n = int(round(len(data) * target_sr / sr))
            if n < 1:
                n = 1
            idx = np.linspace(0, len(data) - 1, n)
            data = np.interp(idx, np.arange(len(data)), data).astype(np.float32)
        # 写 WAV (PCM_16) 到内存
        buf = io.BytesIO()
        sf.write(buf, data, target_sr, format="WAV", subtype="PCM_16")
        duration = len(data) / target_sr
        return buf.getvalue(), duration

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        try:
            # 1. 读音频并转 WAV mono 16kHz
            audio_bytes = request.audio_file.read()
            if not audio_bytes:
                raise TranscriptionError("音频为空")
            wav_bytes, duration = self._to_wav_mono_16k(audio_bytes, self.sample_rate)
            if duration > _MAX_DURATION_SECONDS:
                raise TranscriptionError(
                    f"fun-asr-flash 同步接口限制 ≤ {_MAX_DURATION_SECONDS}s，"
                    f"当前音频 {duration:.1f}s，请在上层分段或改用异步接口"
                )
            logger.info(
                f"dashscope_funasr: 音频 {duration:.1f}s, {len(wav_bytes)} bytes WAV, "
                f"model={self.model}"
            )

            # 2. 构造 base64 data URI
            audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
            data_uri = f"data:audio/wav;base64,{audio_b64}"

            # 3. 构造请求体（可带上下文增强 system 消息）
            messages: List[Dict[str, Any]] = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": data_uri},
                        }
                    ],
                }
            ]
            prompt = (request.prompt or "").strip()
            if prompt:
                messages.insert(0, {
                    "role": "system",
                    "content": [{"type": "text", "text": prompt}],
                })

            body = {
                "model": self.model,
                "input": {"messages": messages},
                "parameters": {
                    "format": "wav",
                    "sample_rate": self.sample_rate,
                },
            }
            if request.language:
                body["parameters"]["language_hints"] = [request.language]

            # 4. 同步调用
            logger.info("dashscope_funasr: 调用 multimodal-generation 端点…")
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                resp = client.post(self.gen_url, headers=self._headers(), json=body)
            if resp.status_code != 200:
                raise TranscriptionError(
                    f"fun-asr-flash 调用失败 [{resp.status_code}]: {resp.text[:500]}"
                )
            data = resp.json()

            # DashScope 同步端点可能以 HTTP 200 返回业务错误载荷
            # （{"code": "...", "message": "..."} 且无 output），必须显式识别
            if isinstance(data, dict) and data.get("code") and not data.get("output"):
                raise TranscriptionError(
                    f"fun-asr-flash 业务失败 [{data.get('code')}]: "
                    f"{data.get('message') or str(data)[:300]}"
                )

            # 5. 解析响应
            return self._parse_response(data, duration, request.language)

        except TranscriptionError:
            raise
        except Exception as e:
            logger.error(f"dashscope_funasr 转写失败: {e}")
            raise TranscriptionError(f"fun-asr-flash 识别失败: {e}") from e

    def _parse_response(
        self, data: Dict[str, Any], duration: float, language: Optional[str]
    ) -> TranscriptionResponse:
        """解析 multimodal-generation 响应。

        实际响应结构（嵌套两层 output）：
            {
              "output": {
                "output": {
                  "text": "...",
                  "sentence": {"text": "...", "begin_time":..., "end_time":...,
                               "words": [{"text":..., "begin_time":..., "end_time":...}]}
                },
                "text": "..."  # 文档提及，实测为嵌套层
              }
            }
        """
        outer = data.get("output") or {}
        inner = outer.get("output") or {}

        # 文本：优先嵌套层，回退外层
        full_text = (inner.get("text") or outer.get("text") or "").strip()

        sentence = inner.get("sentence") or outer.get("sentence") or {}
        words_raw = sentence.get("words") or []

        # 组装 segment（句子级 + 词级）
        segments: List[TranscriptionSegment] = []
        sent_text = (sentence.get("text") or "").strip() or full_text
        if sent_text:
            begin = sentence.get("begin_time")
            end = sentence.get("end_time")
            words: Optional[List[Dict[str, Any]]] = None
            if words_raw:
                words = [
                    {
                        "text": w.get("text", ""),
                        "start": (w.get("begin_time") or 0) / 1000.0,
                        "end": (w.get("end_time") or 0) / 1000.0,
                        "punctuation": w.get("punctuation", ""),
                    }
                    for w in words_raw
                    if w.get("text")
                ]
            segments.append(TranscriptionSegment(
                text=sent_text,
                start_time=begin / 1000.0 if begin is not None else None,
                end_time=end / 1000.0 if end is not None else None,
                words=words,
            ))

        logger.info(
            f"dashscope_funasr: 解析完成 {len(segments)} 句, {len(full_text)} 字"
        )
        return TranscriptionResponse(
            text=full_text,
            segments=segments or None,
            duration=duration,
            language=language,
            provider=self.PROVIDER_NAME,
            model=self.model,
            raw_response=data,
        )

    def health_check(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {"type": "string", "description": "DashScope API Key"},
                "model": {
                    "type": "string",
                    "default": "fun-asr-flash-2026-06-15",
                    "description": "录音文件识别模型名",
                },
                "sample_rate": {
                    "type": "integer",
                    "default": 16000,
                    "description": "音频采样率（fun-asr-flash 推荐 16000）",
                },
            },
        }
