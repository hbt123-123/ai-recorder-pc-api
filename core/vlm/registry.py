"""VLM 连接器注册表。

复刻 core.transcription.registry 的工厂模式，管理 VLM 连接器的注册与
活动实例初始化。配置从环境变量读取。
"""

import os
import threading
import logging
from typing import Dict, Any, Optional, Type, List

from .base import BaseVLMConnector, VLMRequest, VLMResponse, VLMConfigurationError

logger = logging.getLogger(__name__)


class VLMConnectorRegistry:
    """VLM 连接器注册表（单例）。"""

    _instance = None
    _connectors: Dict[str, Type[BaseVLMConnector]] = {}
    _active_connector: Optional[BaseVLMConnector] = None
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
        """注册内置连接器。glm_vl 主用（GLM + Qwen 备用切换），qwen_vl 保留。"""
        from .connectors.glm_vl import GlmVlConnector
        from .connectors.qwen_vl import QwenVLConnector
        self.register('glm_vl', GlmVlConnector)
        self.register('qwen_vl', QwenVLConnector)

    def register(self, name: str, connector_class: Type[BaseVLMConnector]):
        self._connectors[name] = connector_class
        logger.debug(f"Registered VLM connector: {name}")

    def get_connector_class(self, name: str) -> Type[BaseVLMConnector]:
        if name not in self._connectors:
            raise VLMConfigurationError(
                f"Unknown VLM connector: {name}. Available: {list(self._connectors.keys())}"
            )
        return self._connectors[name]

    def create_connector(self, name: str, config: Dict[str, Any]) -> BaseVLMConnector:
        return self.get_connector_class(name)(config)

    def list_connectors(self) -> List[Dict[str, Any]]:
        return [
            {
                'name': name,
                'provider_name': cls.PROVIDER_NAME,
                'capabilities': [c.name for c in cls.CAPABILITIES],
            }
            for name, cls in self._connectors.items()
        ]

    def initialize_from_env(self) -> BaseVLMConnector:
        """从环境变量初始化活动连接器（持生命周期锁，RLock 可重入）。

        优先级：
        1. VLM_PROVIDER 显式指定（glm_vl 主用 / qwen_vl 备用直连）
        2. GLM_API_KEY 已设置 -> glm_vl
        3. 默认 glm_vl（主用 GLM + Qwen 备用切换）
        """
        with self._lifecycle_lock:
          return self._initialize_from_env_unlocked()

    def _initialize_from_env_unlocked(self) -> BaseVLMConnector:
        name = os.environ.get('VLM_PROVIDER', '').strip().lower()
        if not name:
            if os.environ.get('GLM_API_KEY', '').strip():
                name = 'glm_vl'
                logger.info("Auto-detected glm_vl from GLM_API_KEY")
            else:
                name = 'glm_vl'
                logger.info("Using default VLM connector: glm_vl")

        try:
            # 配置构建也纳入契约：int 解析等失败同样包装为 VLMConfigurationError
            config = self._build_config_from_env(name)
            self._active_connector = self.create_connector(name, config)
            self._connector_name = name
            logger.info(f"Initialized VLM connector: {name}")
            return self._active_connector
        except Exception as e:
            logger.error(f"Failed to initialize VLM connector '{name}': {e}")
            raise VLMConfigurationError(f"Failed to initialize VLM connector '{name}': {e}") from e

    def _build_config_from_env(self, name: str) -> Dict[str, Any]:
        if name == 'glm_vl':
            # 主用 GLM + 备用 Qwen-VL（fallback_api_key 空则禁用备用）
            return {
                'api_key': os.environ.get('GLM_API_KEY', ''),
                'base_url': os.environ.get('GLM_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4'),
                'model': os.environ.get('GLM_VLM_MODEL', 'glm-4.6v-flash'),
                'max_tokens': int(os.environ.get('VLM_MAX_TOKENS', '500') or '500'),
                'fallback_api_key': os.environ.get('DASHSCOPE_API_KEY', ''),
                'fallback_base_url': os.environ.get(
                    'VLM_FALLBACK_BASE_URL',
                    'https://dashscope.aliyuncs.com/compatible-mode/v1',
                ),
                'fallback_model': os.environ.get('VLM_FALLBACK_MODEL', 'qwen-vl-max'),
            }
        if name == 'qwen_vl':
            return {
                'api_key': os.environ.get('DASHSCOPE_API_KEY', ''),
                'base_url': os.environ.get('VLM_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1'),
                'model': os.environ.get('VLM_MODEL', 'qwen-vl-max'),
                'max_tokens': int(os.environ.get('VLM_MAX_TOKENS', '500') or '500'),
            }
        if name == 'minicpm_v':
            # 本地 MiniCPM-V（connector 类在 pc_local 注册到本 registry）
            return {
                'model_path': os.environ.get('MINICPM_MODEL_PATH', './models/MiniCPM-V-2.6-int4'),
                'device': os.environ.get('MINICPM_DEVICE', 'auto'),
                'dtype': os.environ.get('MINICPM_DTYPE', 'float16'),
            }
        raise VLMConfigurationError(f"No config builder for VLM connector: {name}")

    def get_active_connector(self) -> BaseVLMConnector:
        with self._lifecycle_lock:
            if not self._active_connector:
                self.initialize_from_env()
            return self._active_connector

    def get_active_connector_name(self) -> str:
        with self._lifecycle_lock:
            if not self._active_connector:
                self.initialize_from_env()
            return self._connector_name

    def reinitialize(self) -> BaseVLMConnector:
        with self._lifecycle_lock:
            old = self._active_connector
            if old is not None:
                close = getattr(old, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as e:
                        logger.warning(f"关闭旧 VLM 连接器失败: {e}")
            self._active_connector = None
            self._connector_name = ""
            return self.initialize_from_env()


_registry: Optional[VLMConnectorRegistry] = None


_registry_lock = threading.Lock()


def get_registry() -> VLMConnectorRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = VLMConnectorRegistry()
    return _registry


vlm_connector_registry = get_registry()


def describe_image(request: VLMRequest) -> VLMResponse:
    """用活动 VLM 连接器生成图片描述。"""
    return get_registry().get_active_connector().describe_image(request)


def get_vlm_connector() -> BaseVLMConnector:
    """获取活动 VLM 连接器。"""
    return get_registry().get_active_connector()
