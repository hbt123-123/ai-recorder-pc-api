"""
Connector registry for managing transcription connectors.

Provides factory pattern for creating and managing transcription connectors,
with auto-detection from environment variables.

改造自 speakr：去掉了 Flask-SQLAlchemy 的 SystemSetting DB fallback，
所有配置改为纯环境变量读取。内置注册 openai_whisper / asr_endpoint /
dashscope_realtime / volc_doubao_file / dashscope_funasr 五个连接器；
faster_whisper_local 由 PC 本地版注册。
"""

import os
import logging
import threading
from typing import Dict, Any, Optional, Type, List

from .base import BaseTranscriptionConnector, TranscriptionRequest, TranscriptionResponse
from .exceptions import ConfigurationError

logger = logging.getLogger(__name__)


class ConnectorRegistry:
    """
    Registry for managing transcription connectors.

    Singleton pattern - use get_registry() to get the shared instance.

    线程安全：初始化/重置路径由 ``_lifecycle_lock`` 保护。GUI 主线程切档时
    调 reinitialize()，同时 QThread worker 可能正调 get_active_connector()，
    无锁的 check-then-act 会重复构造连接器并相互覆盖。
    """

    _instance = None
    _connectors: Dict[str, Type[BaseTranscriptionConnector]] = {}
    _active_connector: Optional[BaseTranscriptionConnector] = None
    _connector_name: str = ""
    _initialized: bool = False
    _instance_lock = threading.Lock()
    _lifecycle_lock = threading.RLock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self):
        with self._lifecycle_lock:
            if self._initialized:
                return
            self._register_builtin_connectors()
            self._initialized = True

    def _register_builtin_connectors(self):
        """Register all built-in connectors."""
        from .connectors.openai_whisper import OpenAIWhisperConnector
        from .connectors.asr_endpoint import ASREndpointConnector
        from .connectors.dashscope_realtime import DashScopeRealtimeConnector
        from .connectors.volc_doubao_file import VolcDoubaoFileConnector
        from .connectors.dashscope_funasr import DashScopeFunASRConnector

        self.register('openai_whisper', OpenAIWhisperConnector)
        self.register('asr_endpoint', ASREndpointConnector)
        self.register('dashscope_realtime', DashScopeRealtimeConnector)
        self.register('volc_doubao_file', VolcDoubaoFileConnector)
        self.register('dashscope_funasr', DashScopeFunASRConnector)

    def register(self, name: str, connector_class: Type[BaseTranscriptionConnector]):
        """
        Register a connector class.

        Args:
            name: Unique name for the connector
            connector_class: The connector class to register
        """
        self._connectors[name] = connector_class
        logger.debug(f"Registered transcription connector: {name}")

    def get_connector_class(self, name: str) -> Type[BaseTranscriptionConnector]:
        """
        Get a connector class by name.

        Args:
            name: The connector name

        Returns:
            The connector class

        Raises:
            ConfigurationError: If connector not found
        """
        if name not in self._connectors:
            raise ConfigurationError(
                f"Unknown connector: {name}. Available: {list(self._connectors.keys())}"
            )
        return self._connectors[name]

    def create_connector(self, name: str, config: Dict[str, Any]) -> BaseTranscriptionConnector:
        """
        Create a connector instance.

        Args:
            name: The connector name
            config: Configuration dict for the connector

        Returns:
            Configured connector instance
        """
        connector_class = self.get_connector_class(name)
        return connector_class(config)

    def list_connectors(self) -> List[Dict[str, Any]]:
        """
        List all registered connectors with their capabilities.

        Returns:
            List of connector info dicts
        """
        result = []
        for name, cls in self._connectors.items():
            result.append({
                'name': name,
                'provider_name': cls.PROVIDER_NAME,
                'capabilities': [c.name for c in cls.CAPABILITIES],
                'config_schema': cls.get_config_schema()
            })
        return result

    def initialize_from_env(self) -> BaseTranscriptionConnector:
        """
        Initialize the active connector from environment variables.

        Auto-detection priority:
        1. TRANSCRIPTION_CONNECTOR - explicit connector name
        2. ASR_BASE_URL is set - use ASR endpoint
        3. Default to OpenAI Whisper (whisper-1)

        Returns:
            The initialized connector
        """
        # 直接持锁（RLock 可重入）：不依赖"调用方已持锁"的隐式约定，
        # 防止未来出现绕过 get_active_connector/reinitialize 的直接调用
        with self._lifecycle_lock:
            connector_name = os.environ.get('TRANSCRIPTION_CONNECTOR', '').lower().strip()

            if not connector_name:
                # Auto-detect based on existing config for backwards compatibility
                asr_base_url = os.environ.get('ASR_BASE_URL', '').strip()
                use_asr_flag = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'
                transcription_model = os.environ.get('TRANSCRIPTION_MODEL', '').lower()
                whisper_model = os.environ.get('WHISPER_MODEL', '').lower()

                # Deprecation warning for legacy USE_ASR_ENDPOINT flag
                if use_asr_flag:
                    logger.warning(
                        "USE_ASR_ENDPOINT=true is deprecated. "
                        "Set ASR_BASE_URL instead for auto-detection, or use TRANSCRIPTION_CONNECTOR=asr_endpoint"
                    )

                # Priority 2: ASR endpoint - check ASR_BASE_URL or legacy flag
                if asr_base_url or use_asr_flag:
                    connector_name = 'asr_endpoint'
                    if asr_base_url:
                        logger.info("Auto-detected ASR endpoint from ASR_BASE_URL")
                # Priority 3: OpenAI Whisper (with custom or default model)
                else:
                    connector_name = 'openai_whisper'
                    model = transcription_model or whisper_model or 'whisper-1'
                    logger.info(f"Using OpenAI Whisper connector with model: {model}")

            # 配置构造同样纳入 try：其中多处 int(env) 解析可能抛 ValueError，
            # 必须按契约包装为 ConfigurationError，调用方才能统一捕获。
            try:
                config = self._build_config_from_env(connector_name)
                self._active_connector = self.create_connector(connector_name, config)
                self._connector_name = connector_name

                logger.info(f"Initialized transcription connector: {connector_name}")
                logger.info(f"Capabilities: {[c.name for c in self._active_connector.get_capabilities()]}")

                return self._active_connector

            except ConfigurationError:
                raise
            except Exception as e:
                logger.error(f"Failed to initialize connector '{connector_name}': {e}")
                raise ConfigurationError(f"Failed to initialize connector '{connector_name}': {e}") from e

    def _get_asr_timeout(self) -> int:
        """
        Get ASR timeout from environment variables.

        Priority:
        1. ASR_TIMEOUT environment variable
        2. asr_timeout_seconds environment variable (legacy)
        3. Default: 1800 seconds (30 minutes)

        Note: speakr 原版在此处还会 fallback 到 SystemSetting (数据库)，
        core/ 去掉了数据库依赖，仅用环境变量 + 默认值。
        """
        env_timeout = os.environ.get('ASR_TIMEOUT') or os.environ.get('asr_timeout_seconds')
        if env_timeout:
            return int(env_timeout)

        # Default: 30 minutes
        return 1800

    def _build_config_from_env(self, connector_name: str) -> Dict[str, Any]:
        """
        Build connector config from environment variables.

        Args:
            connector_name: The connector to build config for

        Returns:
            Configuration dict
        """
        if connector_name == 'asr_endpoint':
            base_url = os.environ.get('ASR_BASE_URL', '')
            if base_url:
                base_url = base_url.split('#')[0].strip()

            return {
                'base_url': base_url,
                'timeout': self._get_asr_timeout(),
                'diarize': os.environ.get('ASR_DIARIZE', 'true').lower() == 'true',
                'return_speaker_embeddings': os.environ.get('ASR_RETURN_SPEAKER_EMBEDDINGS', 'false').lower() == 'true'
            }

        elif connector_name == 'dashscope_realtime':
            # DashScope paraformer-realtime-v2 WebSocket 实时 ASR（纯客户端）
            lang = (os.environ.get('TRANSCRIPTION_LANGUAGE', '')
                    or os.environ.get('WHISPER_LANGUAGE', ''))
            model = (os.environ.get('TRANSCRIPTION_MODEL', '')
                     or 'paraformer-realtime-v2')
            # 8k-v2 模型用 8000，其余默认 16000
            default_sr = 8000 if '8k' in model.lower() else 16000
            sample_rate = int(os.environ.get('TRANSCRIPTION_SAMPLE_RATE', '') or default_sr)
            return {
                'api_key': os.environ.get('TRANSCRIPTION_API_KEY', ''),
                'model': model,
                'sample_rate': sample_rate,
                'language': lang,
            }

        elif connector_name == 'volc_doubao_file':
            # 火山豆包录音文件识别 2.0（submit+query，音频经服务器托管）
            return {
                'app_id': os.environ.get('VOLC_APP_ID', ''),
                'access_token': os.environ.get('VOLC_ACCESS_TOKEN', ''),
                'resource_id': os.environ.get('VOLC_RESOURCE_ID', 'volc.seedasr.auc'),
            }

        elif connector_name == 'dashscope_funasr':
            # 阿里百炼 fun-asr-flash-2026-06-15 同步录音文件识别
            # （multimodal-generation 端点，base64 内联，≤5min）
            return {
                'api_key': os.environ.get('TRANSCRIPTION_API_KEY', ''),
                'model': (os.environ.get('TRANSCRIPTION_MODEL', '')
                          or 'fun-asr-flash-2026-06-15'),
                'sample_rate': int(os.environ.get('TRANSCRIPTION_SAMPLE_RATE', '') or 16000),
            }

        elif connector_name == 'faster_whisper_local':
            # 本地 faster-whisper（connector 类在 pc_local 注册到本 registry）
            return {
                'model_size': os.environ.get('WHISPER_MODEL_SIZE', 'large-v3'),
                'device': os.environ.get('WHISPER_DEVICE', 'auto'),
                'compute_type': os.environ.get('WHISPER_COMPUTE_TYPE', 'int8'),
                'language': os.environ.get('WHISPER_LANGUAGE', '') or None,
                'download_root': os.environ.get('WHISPER_DOWNLOAD_ROOT', '') or None,
                'beam_size': int(os.environ.get('WHISPER_BEAM_SIZE', '5') or '5'),
            }

        else:  # openai_whisper (default)
            base_url = os.environ.get('TRANSCRIPTION_BASE_URL', '')
            if base_url:
                base_url = base_url.split('#')[0].strip()
            # TRANSCRIPTION_MODEL takes priority for custom Whisper variants
            model = os.environ.get('TRANSCRIPTION_MODEL', '') or os.environ.get('WHISPER_MODEL', 'whisper-1')

            return {
                'api_key': os.environ.get('TRANSCRIPTION_API_KEY', ''),
                'base_url': base_url or None,
                'model': model
            }

    def get_active_connector(self) -> BaseTranscriptionConnector:
        """
        Get the currently active connector.

        Initializes from environment if not already initialized.

        Returns:
            The active connector
        """
        with self._lifecycle_lock:
            if not self._active_connector:
                self.initialize_from_env()
            return self._active_connector

    def get_active_connector_name(self) -> str:
        """Get the name of the currently active connector."""
        with self._lifecycle_lock:
            if not self._active_connector:
                self.initialize_from_env()
            return self._connector_name

    def reinitialize(self) -> BaseTranscriptionConnector:
        """
        Force re-initialization of the connector.

        Useful when environment variables have changed.

        Returns:
            The newly initialized connector
        """
        with self._lifecycle_lock:
            old = self._active_connector
            # 释放旧连接器持有的资源（如 httpx 连接池），避免反复切档累积 socket
            if old is not None:
                close = getattr(old, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as e:
                        logger.warning(f"关闭旧转写连接器失败: {e}")
            self._active_connector = None
            self._connector_name = ""
            return self.initialize_from_env()


# Global registry instance
_registry: Optional[ConnectorRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> ConnectorRegistry:
    """Get the global connector registry."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ConnectorRegistry()
    return _registry


# Convenience aliases
connector_registry = get_registry()


def transcribe(request: TranscriptionRequest) -> TranscriptionResponse:
    """
    Transcribe audio using the active connector.

    This is a convenience function that uses the global registry.

    Args:
        request: The transcription request

    Returns:
        Transcription response
    """
    connector = get_registry().get_active_connector()
    return connector.transcribe(request)


def get_connector() -> BaseTranscriptionConnector:
    """Get the active transcription connector."""
    return get_registry().get_active_connector()


def supports_diarization() -> bool:
    """Check if the active connector supports diarization."""
    return get_registry().get_active_connector().supports_diarization
