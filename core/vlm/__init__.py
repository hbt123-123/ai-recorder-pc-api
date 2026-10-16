"""VLM 服务包。

基于连接器的视觉语言模型（VLM）架构，复刻 core.transcription 模式。

Usage:
    from core.vlm import describe_image, VLMRequest

    response = describe_image(VLMRequest(
        image_path="photo.jpg",
        prompt="描述这张图片",
        context_text="此刻录音转写文本...",
    ))
    print(response.description)
"""

from .base import (
    VLMCapability,
    VLMRequest,
    VLMResponse,
    BaseVLMConnector,
    VLMError,
    VLMConfigurationError,
)

from .registry import (
    VLMConnectorRegistry,
    get_registry,
    vlm_connector_registry,
    describe_image,
    get_vlm_connector,
)

from .connectors import QwenVLConnector

__all__ = [
    # Base types
    'VLMCapability',
    'VLMRequest',
    'VLMResponse',
    'BaseVLMConnector',

    # Exceptions
    'VLMError',
    'VLMConfigurationError',

    # Registry
    'VLMConnectorRegistry',
    'get_registry',
    'vlm_connector_registry',

    # Convenience functions
    'describe_image',
    'get_vlm_connector',

    # Connectors
    'QwenVLConnector',
]
