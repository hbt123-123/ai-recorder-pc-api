"""GLM-4.6V-Flash 连接器（智谱 BigModel，OpenAI SDK 兼容）+ Qwen-VL 备用切换。

主用：智谱 glm-4.6v-flash（open.bigmodel.cn）
备用：阿里云 qwen-vl-max（dashscope compatible-mode）

主用调用失败（网络/非 2xx/超时/业务错误）时立即切换备用模型，
保证图片描述功能不中断。
"""

import base64
import logging
import mimetypes
import os
from typing import Dict, Any, Set

from openai import OpenAI, APIConnectionError, APITimeoutError, APIStatusError

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


class GlmVlConnector(BaseVLMConnector):
    """GLM-4.6V-Flash 主用 + Qwen-VL-Max 备用的 VLM 连接器。"""

    CAPABILITIES: Set[VLMCapability] = {
        VLMCapability.IMAGE_UNDERSTANDING,
    }
    PROVIDER_NAME = "glm_vl"

    def __init__(self, config: Dict[str, Any]):
        """
        Args:
            config:
                - api_key: BigModel API key（主用，必需）
                - base_url: BigModel endpoint（默认 https://open.bigmodel.cn/api/paas/v4）
                - model: 主用模型名（默认 glm-4.6v-flash）
                - max_tokens: 默认最大输出 token
                - fallback_api_key: 备用 DashScope key（可选）
                - fallback_base_url: 备用 endpoint
                - fallback_model: 备用模型名（默认 qwen-vl-max）
        """
        super().__init__(config)
        self.api_key = config['api_key']
        self.base_url = config.get('base_url', 'https://open.bigmodel.cn/api/paas/v4')
        self.model = config.get('model', 'glm-4.6v-flash')
        self.max_tokens = config.get('max_tokens', 500)

        # 备用 Qwen-VL（无 fallback key 则禁用备用）
        self.fallback_api_key = config.get('fallback_api_key', '')
        self.fallback_base_url = config.get(
            'fallback_base_url',
            'https://dashscope.aliyuncs.com/compatible-mode/v1',
        )
        self.fallback_model = config.get('fallback_model', 'qwen-vl-max')

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        self.fallback_client = (
            OpenAI(api_key=self.fallback_api_key, base_url=self.fallback_base_url)
            if self.fallback_api_key
            else None
        )

    def _validate_config(self) -> None:
        if not self.config.get('api_key'):
            raise VLMConfigurationError("api_key is required for GLM VL connector")

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
        """主用 GLM 生成图片描述；失败立即切换备用 Qwen-VL。"""
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

        # ---- 主用：GLM ----
        try:
            logger.info(f"Sending request to GLM-VL with model: {effective_model}")
            return self._chat(self.client, effective_model, image_url, prompt, max_tokens,
                              provider=self.PROVIDER_NAME)
        except APIStatusError as e:
            # 仅服务端类状态（5xx/429 限流）值得切换备用；4xx 客户端错误
            # （401 无效 key / 400 参数错等）切到 Qwen 也必然失败，
            # 只会白白多一次计费调用并掩盖真实根因
            status = getattr(e, "status_code", None)
            if status is not None and status < 500 and status != 429:
                logger.warning(f"GLM-VL primary failed (client error {status}, no fallback): {e}")
                raise VLMError(f"GLM VL describe failed: {e}") from e
            logger.warning(f"GLM-VL primary failed ({status}), switching to fallback: {e}")
            if not self.fallback_client:
                raise VLMError(f"GLM VL describe failed (no fallback): {e}") from e
        except (APIConnectionError, APITimeoutError) as e:
            # 可重试的网络/超时异常触发备用切换
            # （openai SDK 已将 httpx.TimeoutException 包装为 APITimeoutError，其继承 APIConnectionError）
            logger.warning(f"GLM-VL primary failed, switching to fallback: {e}")
            if not self.fallback_client:
                raise VLMError(f"GLM VL describe failed (no fallback): {e}") from e
        except Exception as e:
            # 其余异常（业务/解析等）不 fallback，直接包装上抛
            logger.warning(f"GLM-VL primary failed (non-retryable, no fallback): {e}")
            raise VLMError(f"GLM VL describe failed: {e}") from e

        # ---- 备用：Qwen-VL ----
        try:
            logger.info(f"Falling back to Qwen-VL with model: {self.fallback_model}")
            return self._chat(self.fallback_client, self.fallback_model, image_url, prompt,
                              max_tokens, provider="qwen_vl_fallback")
        except Exception as e:
            logger.error(f"Both GLM-VL and fallback Qwen-VL failed: {e}")
            raise VLMError(f"VLM describe failed (primary+fallback): {e}") from e

    def _chat(self, client: OpenAI, model: str, image_url: str, prompt: str,
              max_tokens: int, provider: str) -> VLMResponse:
        """统一 OpenAI 兼容 chat 调用。"""
        response = client.chat.completions.create(
            model=model,
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
            provider=provider,
            model=model,
            raw_response=response.model_dump() if hasattr(response, 'model_dump') else None,
        )

    def list_models(self):
        """BigModel 端点暂不支持 models.list，返回静态模型清单。"""
        return [
            {'id': 'glm-4.6v-flash', 'label': 'GLM-4.6V-Flash（主用）', 'owned_by': 'zhipu'},
            {'id': self.fallback_model, 'label': f'{self.fallback_model}（备用）', 'owned_by': 'alibaba'},
        ]

    def health_check(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {"type": "string", "description": "BigModel API key"},
                "base_url": {"type": "string", "default": "https://open.bigmodel.cn/api/paas/v4"},
                "model": {"type": "string", "default": "glm-4.6v-flash"},
                "max_tokens": {"type": "integer", "default": 500},
                "fallback_api_key": {"type": "string", "description": "DashScope fallback key"},
                "fallback_model": {"type": "string", "default": "qwen-vl-max"},
            },
        }
