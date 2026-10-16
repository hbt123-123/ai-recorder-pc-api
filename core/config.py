"""统一配置管理。

从环境变量读取配置，提供全局访问。三版本共享同一套配置契约：
- PC API 版：TRANSCRIPTION_API_KEY / DASHSCOPE_API_KEY
- PC 本地版：本地模型路径 + 云降级 key
- 安卓版：独立 Kotlin，但 API 字段与本配置对齐

环境变量一览：
    # 转写
    TRANSCRIPTION_CONNECTOR   openai_whisper | asr_endpoint | faster_whisper_local
    TRANSCRIPTION_API_KEY     云 API key
    TRANSCRIPTION_BASE_URL    OpenAI 兼容 base_url
    TRANSCRIPTION_MODEL       模型名
    ASR_BASE_URL              自建 ASR endpoint
    ASR_TIMEOUT               ASR 超时秒数
    WHISPER_MODEL_SIZE        本地 faster-whisper 模型大小
    WHISPER_DEVICE            auto | cuda | cpu

    # VLM
    VLM_PROVIDER              qwen_vl | minicpm_v
    DASHSCOPE_API_KEY         阿里云 DashScope key（Qwen-VL / LLM 共用）
    VLM_BASE_URL              OpenAI 兼容 base_url
    VLM_MODEL                 VLM 模型名
    MINICPM_MODEL_PATH        本地 MiniCPM-V 模型路径

    # LLM（融合笔记）
    LLM_API_KEY               文本 LLM key（默认 DeepSeek，由 credentials 注入）
    LLM_BASE_URL              OpenAI 兼容 base_url（默认 https://api.deepseek.com）
    LLM_MODEL                 文本 LLM 模型名（默认 deepseek-v4-flash）
    LLM_MAX_TOKENS            最大输出 token

    # 存储
    FILE_STORAGE_BACKEND      local（MVP 仅支持 local）
    STORAGE_LOCAL_ROOT        本地存储根目录

    # 数据库
    DATABASE_URL              SQLAlchemy URL
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TranscriptionConfig:
    """转写引擎配置。"""
    provider: str = "openai_whisper"
    api_key: str = ""
    base_url: Optional[str] = None
    model: str = "whisper-1"
    # ASR endpoint 专用
    asr_base_url: str = ""
    asr_timeout: int = 1800
    asr_diarize: bool = True
    # 本地 faster-whisper 专用（阶段 3）
    local_model_size: str = "large-v3"
    local_device: str = "auto"  # auto | cuda | cpu
    local_compute_type: str = "int8"


@dataclass
class VLMConfig:
    """VLM 引擎配置。"""
    provider: str = "qwen_vl"
    api_key: str = ""
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen-vl-max"
    max_tokens: int = 500
    # 本地 MiniCPM-V 专用（阶段 3）
    local_model_path: str = "./models/MiniCPM-V-2.6-int4"
    # 显存不足时的云降级
    fallback_provider: Optional[str] = None
    fallback_api_key: str = ""
    fallback_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    fallback_model: str = "qwen-vl-max"


@dataclass
class LLMConfig:
    """文本 LLM 配置（融合笔记生成用）。

    主用智谱 GLM-4.7-Flash，备用 DeepSeek（主用失败自动切换）。
    OpenAI 兼容，由 pc_api/credentials.py 注入 LLM_API_KEY。
    """
    api_key: str = ""
    base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    model: str = "glm-4.7-flash"
    max_tokens: int = 2000
    # 备用 LLM（主用失败时切换）
    fallback_api_key: str = ""
    fallback_base_url: str = "https://api.deepseek.com"
    fallback_model: str = "deepseek-v4-flash"


@dataclass
class AppConfig:
    """应用全局配置。

    注：存储相关配置（FILE_STORAGE_BACKEND 等）由 core/storage/factory.py
    直接从环境变量读取，不在此重复建模（避免两套事实源漂移）。
    """
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    vlm: VLMConfig = field(default_factory=VLMConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    database_url: str = "sqlite:///./data/airecorder.db"


def _env_int(name: str, default: int) -> int:
    """读整型环境变量；非法值给出含变量名的清晰错误（而非裸 ValueError 栈）。"""
    raw = (os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(
            f"环境变量 {name} 的值 {raw!r} 不是合法整数（默认应为 {default}）"
        ) from e


def load_config() -> AppConfig:
    """从环境变量加载配置。"""
    return AppConfig(
        transcription=TranscriptionConfig(
            provider=os.environ.get("TRANSCRIPTION_CONNECTOR", "openai_whisper").strip() or "openai_whisper",
            api_key=os.environ.get("TRANSCRIPTION_API_KEY", ""),
            base_url=os.environ.get("TRANSCRIPTION_BASE_URL", "").strip() or None,
            model=os.environ.get("TRANSCRIPTION_MODEL", "whisper-1"),
            asr_base_url=os.environ.get("ASR_BASE_URL", "").strip(),
            asr_timeout=_env_int("ASR_TIMEOUT", 1800),
            asr_diarize=os.environ.get("ASR_DIARIZE", "true").lower() == "true",
            local_model_size=os.environ.get("WHISPER_MODEL_SIZE", "large-v3"),
            local_device=os.environ.get("WHISPER_DEVICE", "auto"),
            local_compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
        ),
        vlm=VLMConfig(
            provider=os.environ.get("VLM_PROVIDER", "qwen_vl").strip() or "qwen_vl",
            api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
            base_url=os.environ.get("VLM_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            model=os.environ.get("VLM_MODEL", "qwen-vl-max"),
            max_tokens=_env_int("VLM_MAX_TOKENS", 500),
            local_model_path=os.environ.get("MINICPM_MODEL_PATH", "./models/MiniCPM-V-2.6-int4"),
            fallback_provider=os.environ.get("VLM_FALLBACK_PROVIDER", "").strip() or None,
            fallback_api_key=os.environ.get("VLM_FALLBACK_API_KEY", ""),
            fallback_base_url=os.environ.get("VLM_FALLBACK_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            fallback_model=os.environ.get("VLM_FALLBACK_MODEL", "qwen-vl-max"),
        ),
        llm=LLMConfig(
            api_key=os.environ.get("LLM_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", ""),
            base_url=os.environ.get("LLM_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4",
            model=os.environ.get("LLM_MODEL", "glm-4.7-flash"),
            max_tokens=_env_int("LLM_MAX_TOKENS", 2000),
            fallback_api_key=os.environ.get("LLM_FALLBACK_API_KEY", ""),
            fallback_base_url=os.environ.get("LLM_FALLBACK_BASE_URL") or "https://api.deepseek.com",
            fallback_model=os.environ.get("LLM_FALLBACK_MODEL", "deepseek-v4-flash"),
        ),
        database_url=os.environ.get("DATABASE_URL", "sqlite:///./data/airecorder.db"),
    )


_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """获取全局配置单例。"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """重置配置单例（环境变量变更后重新加载）。"""
    global _config
    _config = None
