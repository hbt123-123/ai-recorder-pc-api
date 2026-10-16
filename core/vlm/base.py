"""VLM 连接器基类与数据类型。

复刻 core.transcription 的连接器架构（base.py），为视觉语言模型（VLM）
提供统一的抽象层。

连接器实现：
- qwen_vl: Qwen3-VL-Flash（云端，OpenAI SDK 兼容）
- minicpm_v: MiniCPM-V 2.6（本地，阶段 3 在 pc_local/ 实现）
- doubao_vlm: 豆包 Seed-1.6-Flash（云端兜底，阶段 3 扩展）
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Dict, Any, Set, List


class VLMCapability(Enum):
    """VLM 连接器能力声明。"""
    IMAGE_UNDERSTANDING = auto()   # 图片理解
    VIDEO_UNDERSTANDING = auto()   # 视频理解
    STREAMING = auto()             # 流式输出


@dataclass
class VLMRequest:
    """标准化 VLM 请求。

    image_path 与 image_base64 二选一：本地路径优先，其次 base64 字符串
    （不带 data: 前缀）。
    """
    image_path: Optional[str] = None       # 本地图片路径
    image_base64: Optional[str] = None     # 或 base64（不带 data: 前缀）
    prompt: str = ""                        # 提示词
    context_text: Optional[str] = None      # 关联的转写文本（时间轴对齐）
    max_tokens: int = 500
    model: Optional[str] = None             # per-request model override


@dataclass
class VLMResponse:
    """标准化 VLM 响应。"""
    description: str                                # 图片描述
    raw_response: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, int]] = None          # token 用量
    provider: str = ""
    model: str = ""


class VLMError(Exception):
    """VLM 错误基类。"""
    pass


class VLMConfigurationError(VLMError):
    """VLM 配置错误（缺失或无效）。"""
    pass


class BaseVLMConnector(ABC):
    """VLM 连接器抽象基类。"""

    # 类级能力声明，子类应覆盖
    CAPABILITIES: Set[VLMCapability] = set()
    PROVIDER_NAME: str = "unknown"

    def __init__(self, config: Dict[str, Any]):
        """
        Args:
            config: 连接器配置 dict
        """
        self.config = config
        self._validate_config()

    @abstractmethod
    def _validate_config(self) -> None:
        """校验配置。失败抛 VLMConfigurationError。"""
        pass

    @abstractmethod
    def describe_image(self, request: VLMRequest) -> VLMResponse:
        """生成图片描述。

        Args:
            request: 标准 VLM 请求

        Returns:
            VLMResponse

        Raises:
            VLMError: 描述生成失败
        """
        pass

    def supports(self, capability: VLMCapability) -> bool:
        """检查是否支持某能力。"""
        return capability in self.CAPABILITIES

    def get_capabilities(self) -> Set[VLMCapability]:
        """获取所有支持的能力。"""
        return self.CAPABILITIES.copy()

    @property
    def supports_image(self) -> bool:
        return VLMCapability.IMAGE_UNDERSTANDING in self.CAPABILITIES

    @property
    def supports_video(self) -> bool:
        return VLMCapability.VIDEO_UNDERSTANDING in self.CAPABILITIES

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        """返回配置 JSON schema（供 UI / 校验）。"""
        return {}

    def list_models(self) -> List[Dict[str, Any]]:
        """列举上游模型。默认未实现。"""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement list_models()"
        )

    def health_check(self) -> bool:
        """检查连接器是否可用。"""
        return True
