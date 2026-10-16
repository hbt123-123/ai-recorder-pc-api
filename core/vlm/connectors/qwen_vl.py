"""Qwen3-VL-Flash 连接器（云端，OpenAI SDK 兼容）。

通过阿里云 DashScope 的 OpenAI 兼容模式接入 Qwen-VL 系列。
文档 7.2：仅需改 base_url + api_key 即可用 openai SDK。
"""

import base64
import logging
import mimetypes
import os
from typing import Dict, Any, Set

from openai import OpenAI

from ..base import (
    BaseVLMConnector,
    VLMCapability,
    VLMRequest,
    VLMResponse,
    VLMError,
    VLMConfigurationError,
)

logger = logging.getLogger(__name__)

# 图片大小上限：20MB
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _sniff_mime(data: bytes) -> str:
    """按魔数嗅探图片 MIME 类型，默认 jpeg。

    Args:
        data: 图片二进制头部字节（至少 12 字节可识别 webp）

    Returns:
        MIME 字符串（image/jpeg / image/png / image/webp / image/gif）
    """
    if data.startswith(b'\xff\xd8'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG'):
        return 'image/png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data.startswith(b'GIF8'):
        return 'image/gif'
    return 'image/jpeg'


def _sniff_mime_from_base64(b64_str: str) -> str:
    """从 base64 字符串头部解码少量字节后嗅探 MIME 类型。"""
    try:
        head = base64.b64decode(b64_str[:16] + '===', validate=False)
    except Exception:
        # 嗅探失败不影响主流程，退回 jpeg 默认值
        return 'image/jpeg'
    return _sniff_mime(head)


class QwenVLConnector(BaseVLMConnector):
    """Qwen-VL 云端连接器（DashScope OpenAI 兼容模式）。"""

    CAPABILITIES: Set[VLMCapability] = {
        VLMCapability.IMAGE_UNDERSTANDING,
    }
    PROVIDER_NAME = "qwen_vl"

    def __init__(self, config: Dict[str, Any]):
        """
        Args:
            config:
                - api_key: DashScope API key（必需）
                - base_url: 兼容模式 endpoint（默认 dashscope）
                - model: 模型名（默认 qwen-vl-max）
                - max_tokens: 默认最大输出 token
        """
        super().__init__(config)
        self.api_key = config['api_key']
        self.base_url = config.get('base_url', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
        self.model = config.get('model', 'qwen-vl-max')
        self.max_tokens = config.get('max_tokens', 500)
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def _validate_config(self) -> None:
        if not self.config.get('api_key'):
            raise VLMConfigurationError("api_key is required for Qwen VL connector")

    @staticmethod
    def _load_image_as_data_url(image_path: str) -> str:
        """读取本地图片并转为 data URL。

        Raises:
            VLMError: 文件为空或超过 20MB 上限。
        """
        size = os.path.getsize(image_path)
        if size == 0:
            raise VLMError(f"图片文件为空: {image_path}")
        if size > _MAX_IMAGE_BYTES:
            mb = round(size / (1024 * 1024), 1)
            raise VLMError(f"图片过大({mb}MB)，上限20MB: {image_path}")
        with open(image_path, 'rb') as f:
            raw = f.read()
        # 优先按魔数嗅探，嗅探不出再退回扩展名猜测
        mime = _sniff_mime(raw)
        if mime == 'image/jpeg':
            guessed, _ = mimetypes.guess_type(image_path)
            mime = guessed if guessed else mime
        b64 = base64.b64encode(raw).decode('utf-8')
        return f"data:{mime};base64,{b64}"

    def describe_image(self, request: VLMRequest) -> VLMResponse:
        """调用 Qwen-VL 生成图片描述。"""
        # 准备图片 data URL
        if request.image_path:
            if not os.path.exists(request.image_path):
                raise VLMError(f"Image not found: {request.image_path}")
            image_url = self._load_image_as_data_url(request.image_path)
        elif request.image_base64:
            # 解码头部字节嗅探真实格式，避免硬编码 jpeg 前缀误标
            mime = _sniff_mime_from_base64(request.image_base64)
            image_url = f"data:{mime};base64,{request.image_base64}"
        else:
            raise VLMError("No image provided in VLMRequest (set image_path or image_base64)")

        # 构造提示词：结合转写上下文
        prompt = request.prompt or "请描述这张图片的内容。"
        if request.context_text:
            prompt = (
                f"{prompt}\n\n"
                f"以下是录音中此刻附近的转写文本，请结合它理解图片语境：\n"
                f"{request.context_text}"
            )

        effective_model = request.model or self.model
        max_tokens = request.max_tokens or self.max_tokens

        try:
            logger.info(f"Sending request to Qwen-VL with model: {effective_model}")
            response = self.client.chat.completions.create(
                model=effective_model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": prompt},
                    ],
                }],
                max_tokens=max_tokens,
            )

            description = ""
            if response.choices:
                description = response.choices[0].message.content or ""

            usage = None
            if response.usage:
                usage = {
                    'prompt_tokens': response.usage.prompt_tokens,
                    'completion_tokens': response.usage.completion_tokens,
                    'total_tokens': response.usage.total_tokens,
                }

            return VLMResponse(
                description=description,
                usage=usage,
                provider=self.PROVIDER_NAME,
                model=effective_model,
                raw_response=response.model_dump() if hasattr(response, 'model_dump') else None,
            )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Qwen VL describe failed: {error_msg}")
            raise VLMError(f"Qwen VL describe failed: {error_msg}") from e

    def list_models(self):
        """列出 dashscope 兼容端点的模型（过滤视觉相关）。"""
        try:
            resp = self.client.models.list()
            result = []
            for m in resp.data:
                mid = getattr(m, 'id', '')
                if mid and ('vl' in mid.lower() or 'vision' in mid.lower()):
                    result.append({'id': mid, 'label': mid, 'owned_by': getattr(m, 'owned_by', 'unknown')})
            return result
        except Exception as e:
            logger.warning(f"qwen_vl list_models failed: {e}")
            return []

    def health_check(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {"type": "string", "description": "DashScope API key"},
                "base_url": {"type": "string", "description": "OpenAI 兼容 endpoint"},
                "model": {"type": "string", "default": "qwen-vl-max"},
                "max_tokens": {"type": "integer", "default": 500},
            },
        }
