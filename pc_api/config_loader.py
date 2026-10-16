"""pc_api 配置加载：从 UserConfig + .env 凭证注入环境变量。

四级 ASR tier（free/realtime/pro_lite/pro_pro）由用户在 SettingsDialog 选择，
凭证定义在仓库根目录 .env，由 pc_api/credentials.py 读取。VLM 主用智谱
GLM-4.6V-Flash（备用百炼 Qwen-VL-Max），LLM 主用智谱 GLM-4.7-Flash
（备用 DeepSeek deepseek-v4-flash）。
"""

import os

from core.user_config import UserConfig, load_user_config

# 凭证（从 .env 加载）
from pc_api import credentials
from core.transcription.tiers import TIER_DEFS, get_tier

# apply_tier_env 上一轮实际注入的 env key。切档时先清理旧 tier 独有的键
# （如 realtime 遗留 TRANSCRIPTION_SAMPLE_RATE、pro_lite 遗留 VOLC_*/AUDIO_HOST_*），
# 防止残留值被其他代码路径意外读取造成跨档位污染。
_injected_tier_env_keys: list = []


def apply_tier_env(tier_id: str, language: str = "") -> None:
    """按 tier_id 注入转写相关环境变量（连接器 + 凭证 + 模型）。

    仅设置转写相关 env，不影响 VLM/LLM 配置，也不重置 core 单例
    （调用方自行决定是否需要 get_registry().reinitialize()）。

    Args:
        tier_id: free / realtime / pro_lite / pro_pro
        language: 转写语言（zh / en / 空=自动检测）

    Raises:
        ValueError: tier_id 不在 TIER_DEFS 中（get_tier 会静默回退 free，
                    必须在入口显式拦截）
    """
    global _injected_tier_env_keys
    if tier_id not in TIER_DEFS:
        raise ValueError(f"未知 ASR tier: {tier_id!r}")
    tier = get_tier(tier_id)

    if tier.tier_id == "free":
        # 免费版:硅基流动 SenseVoiceSmall(OpenAI 兼容)。
        # 注:原 TeleAI/TeleSpeechASR 在 SiliconFlow 端被单独 403(订阅权限问题,
        # 与余额无关)。SenseVoiceSmall 仅返回纯文本(无 segments 时间戳),VAD 分段
        # 转写时单段降级为整段一个 chunk,段起始时间戳由 VAD 状态机提供。
        env = {
            "TRANSCRIPTION_CONNECTOR": "openai_whisper",
            "TRANSCRIPTION_API_KEY": credentials.SILICONFLOW_API_KEY,
            "TRANSCRIPTION_BASE_URL": "https://api.siliconflow.cn/v1",
            "TRANSCRIPTION_MODEL": "FunAudioLLM/SenseVoiceSmall",
        }

    elif tier.tier_id == "realtime":
        # 实时版：阿里百炼 paraformer-realtime-8k-v2（8kHz）
        env = {
            "TRANSCRIPTION_CONNECTOR": "dashscope_realtime",
            "TRANSCRIPTION_MODEL": "paraformer-realtime-8k-v2",
            "TRANSCRIPTION_SAMPLE_RATE": "8000",
        }

    elif tier.tier_id == "pro_lite":
        # 专业版 Lite：火山豆包录音文件识别 2.0（服务器托管音频）
        env = {
            "TRANSCRIPTION_CONNECTOR": "volc_doubao_file",
            "VOLC_APP_ID": credentials.VOLC_APP_ID,
            "VOLC_ACCESS_TOKEN": credentials.VOLC_ACCESS_TOKEN,
            "VOLC_RESOURCE_ID": "volc.seedasr.auc",
            # 服务器音频托管（SFTP 上传后 HTTP 暴露）
            "AUDIO_HOST_HOST": credentials.AUDIO_HOST_HOST,
            "AUDIO_HOST_PORT": str(credentials.AUDIO_HOST_PORT),
            "AUDIO_HOST_USER": credentials.AUDIO_HOST_USER,
            "AUDIO_HOST_REMOTE_DIR": credentials.AUDIO_HOST_REMOTE_DIR,
            "AUDIO_HOST_PUBLIC_BASE": credentials.AUDIO_HOST_PUBLIC_BASE,
        }

    else:  # pro_pro
        # 专业版 Pro：阿里百炼 fun-asr-flash-2026-06-15（已核实：真实模型 ID）
        # （同步 multimodal-generation 端点，base64 内联，≤5min）
        env = {
            "TRANSCRIPTION_CONNECTOR": "dashscope_funasr",
            "TRANSCRIPTION_MODEL": "fun-asr-flash-2026-06-15",
            "TRANSCRIPTION_SAMPLE_RATE": "16000",
        }

    # dashscope 系档位共用百炼 key；无条件注入且不参与跨档清理——
    # 它们是双用途键（TRANSCRIPTION_API_KEY 同时被 VLM 备用通道的
    # DASHSCOPE_API_KEY 语义共用），文件转写页直调本函数（不经 apply_config
    # 的 VLM 段）时若被清理会导致 Qwen 备用静默失效
    env["DASHSCOPE_API_KEY"] = credentials.DASHSCOPE_API_KEY
    if tier.tier_id in ("realtime", "pro_pro"):
        # free 档的 TRANSCRIPTION_API_KEY 走硅基流动（见上方 dict）；
        # dashscope 系连接器（realtime/funasr）从该键读百炼凭证
        env["TRANSCRIPTION_API_KEY"] = credentials.DASHSCOPE_API_KEY
    env["TRANSCRIPTION_LANGUAGE"] = language or ""

    # 清理上一 tier 注入、本 tier 不再使用的键
    for key in _injected_tier_env_keys:
        if key not in env and key in os.environ:
            del os.environ[key]
    for key, value in env.items():
        os.environ[key] = value
    _injected_tier_env_keys = list(env.keys())


def apply_config(user_cfg: UserConfig = None) -> UserConfig:
    """按 UserConfig.asr_tier 注入环境变量，供 core 单例读取。

    Args:
        user_cfg: 用户配置；None 则从 ~/.airecorder/config.json 加载

    Raises:
        ValueError: 无配置
    """
    if user_cfg is None:
        user_cfg = load_user_config()
        if user_cfg is None:
            raise ValueError("无用户配置，请先调用 SettingsDialog")

    # ----- 转写：按 tier 映射连接器 + 凭证 -----
    apply_tier_env(user_cfg.asr_tier, user_cfg.language or "")

    # ----- VLM：主用智谱 GLM-4.6V-Flash，备用阿里云 Qwen-VL-Max（失败自动切换）-----
    os.environ["VLM_PROVIDER"] = "glm_vl"
    os.environ["GLM_API_KEY"] = credentials.BIGMODEL_API_KEY
    os.environ["GLM_BASE_URL"] = credentials.BIGMODEL_BASE_URL
    os.environ["GLM_VLM_MODEL"] = credentials.BIGMODEL_VLM_MODEL
    os.environ["VLM_MAX_TOKENS"] = "500"
    # 备用 Qwen-VL（由 glm_vl 连接器内部在主用失败时切换）
    os.environ["DASHSCOPE_API_KEY"] = credentials.DASHSCOPE_API_KEY
    os.environ["VLM_FALLBACK_MODEL"] = "qwen-vl-max"

    # ----- LLM：主用智谱 GLM-4.7-Flash，备用 DeepSeek（失败自动切换）-----
    os.environ["LLM_API_KEY"] = credentials.BIGMODEL_API_KEY
    os.environ["LLM_BASE_URL"] = credentials.BIGMODEL_BASE_URL
    os.environ["LLM_MODEL"] = credentials.BIGMODEL_LLM_MODEL
    os.environ["LLM_MAX_TOKENS"] = str(user_cfg.llm_max_tokens)
    # 备用 DeepSeek（由 note_fuser 在主用失败时切换）
    os.environ["LLM_FALLBACK_API_KEY"] = credentials.DEEPSEEK_API_KEY
    os.environ["LLM_FALLBACK_BASE_URL"] = "https://api.deepseek.com"
    os.environ["LLM_FALLBACK_MODEL"] = "deepseek-v4-flash"

    # ----- 存储 / 数据库（默认本地）-----
    os.environ.setdefault("FILE_STORAGE_BACKEND", "local")
    os.environ.setdefault("STORAGE_LOCAL_ROOT", "./data/storage")
    os.environ.setdefault("FILE_STORAGE_STAGING_DIR", "./data/staging")
    os.environ.setdefault("DATABASE_URL", "sqlite:///./data/airecorder.db")

    # 重置 core 单例，使新环境变量生效
    from core.config import reset_config
    reset_config()
    from core.storage.service import reset_storage_service_singleton
    reset_storage_service_singleton()
    from core.models import init_engine
    init_engine()

    # 转写连接器需重新初始化（env 变更后）
    from core.transcription.registry import get_registry
    get_registry().reinitialize()

    return user_cfg


def build_realtime_stream_config() -> dict:
    """根据当前 env（apply_config 已为 realtime tier 注入）构造实时会话配置。

    供 RealtimeTranscriptionWorker 构造 DashScopeLiveSession 使用。
    与 dashscope_realtime 连接器的 config 形状一致（api_key/model/
    sample_rate/language）。
    """
    model = os.environ.get("TRANSCRIPTION_MODEL", "") or "paraformer-realtime-8k-v2"
    default_sr = 8000 if "8k" in model.lower() else 16000
    return {
        "api_key": os.environ.get("TRANSCRIPTION_API_KEY", ""),
        "model": model,
        "sample_rate": int(os.environ.get("TRANSCRIPTION_SAMPLE_RATE", "") or default_sr),
        "language": os.environ.get("TRANSCRIPTION_LANGUAGE", "") or None,
    }
