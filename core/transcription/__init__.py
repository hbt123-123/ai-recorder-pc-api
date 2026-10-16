"""
Transcription service package.

Connector-based architecture for speech-to-text transcription.

core/ 版 registry 内置注册的连接器（见 registry._register_builtin_connectors）：
- openai_whisper: OpenAI Whisper API (whisper-1) / OpenAI 兼容端点
- asr_endpoint: 自建 ASR webservice (whisper-asr-webservice, WhisperX)
- dashscope_realtime: 阿里云百炼 paraformer 实时 WebSocket 转写
- volc_doubao_file: 火山豆包录音文件识别（submit+query，音频需外部托管）
- dashscope_funasr: 阿里云百炼 fun-asr 同步录音文件识别（base64 内联，≤5min）

另有 faster_whisper_local 本地连接器由 PC 本地版（pc_local）注册到本 registry；
dashscope_realtime_stream 流式会话实现不经 registry 注册，由 pc_api 实时 worker 直接使用。

Usage:
    from core.transcription import (
        transcribe,
        get_connector,
        TranscriptionRequest,
        TranscriptionResponse,
    )

    with open('audio.wav', 'rb') as f:
        request = TranscriptionRequest(
            audio_file=f,
            filename='audio.wav',
            diarize=True
        )
        response = transcribe(request)
        print(response.text)
        if response.segments:
            for seg in response.segments:
                print(f"[{seg.speaker}]: {seg.text}")
"""

from .base import (
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
    BaseTranscriptionConnector,
    ConnectorSpecifications,
    DEFAULT_SPECIFICATIONS,
)

from .exceptions import (
    TranscriptionError,
    ConfigurationError,
    ProviderError,
    AudioFormatError,
    ChunkingError,
)

from .registry import (
    ConnectorRegistry,
    get_registry,
    connector_registry,
    transcribe,
    get_connector,
    supports_diarization,
)

# 直接从连接器模块导入（connectors/__init__ 刻意不急切导入，
# 以保持 registry 的延迟加载设计）
from .connectors.openai_whisper import OpenAIWhisperConnector
from .connectors.asr_endpoint import ASREndpointConnector

__all__ = [
    # Base types
    'TranscriptionCapability',
    'TranscriptionRequest',
    'TranscriptionResponse',
    'TranscriptionSegment',
    'BaseTranscriptionConnector',
    'ConnectorSpecifications',
    'DEFAULT_SPECIFICATIONS',

    # Exceptions
    'TranscriptionError',
    'ConfigurationError',
    'ProviderError',
    'AudioFormatError',
    'ChunkingError',

    # Registry
    'ConnectorRegistry',
    'get_registry',
    'connector_registry',

    # Convenience functions
    'transcribe',
    'get_connector',
    'supports_diarization',

    # Connectors
    'OpenAIWhisperConnector',
    'ASREndpointConnector',
]
