"""用户运行时配置（PC 两版共享）。

用户安装/打开应用时输入的厂商 + key + 模型，持久化到 ~/.airecorder/config.json。
不硬编码——所有密钥由用户自行输入。

厂商预设（基于《国内三大模型厂商API调研.md》）：
- 阿里云百炼：base_url dashscope，VLM qwen-vl-max，LLM qwen-plus
- 火山引擎：base_url ark，VLM doubao-1.5-vision-pro，LLM doubao-pro-32k
- 讯飞星火：base_url spark-api-open，VLM/LLM 4.0Ultra
- 自定义：用户自填

三家文本/视觉均 OpenAI SDK 兼容（chat/completions）。
语音转写协议各异（火山/讯飞 WebSocket、阿里 DashScope），故 pc_api/安卓转写用
OpenAI 兼容端点（用户填 base_url+key+model，如 OpenAI 或自建 ASR）；pc_local 用本地。
"""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROVIDER_PRESETS = {
    "aliyun": {
        "label": "阿里云百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "vlm_model": "qwen-vl-max",
        "llm_model": "qwen-plus",
    },
    "volc": {
        "label": "火山引擎（豆包）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "vlm_model": "doubao-1.5-vision-pro",
        "llm_model": "doubao-pro-32k",
    },
    "xfyun": {
        "label": "讯飞星火",
        "base_url": "https://spark-api-open.xf-yun.com/v1/",
        "vlm_model": "4.0Ultra",
        "llm_model": "4.0Ultra",
    },
    "custom": {
        "label": "自定义",
        "base_url": "",
        "vlm_model": "",
        "llm_model": "",
    },
}

CONFIG_DIR = Path.home() / ".airecorder"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class UserConfig:
    """用户配置。VLM/LLM 与转写分开配置。"""

    # ----- VLM / LLM（OpenAI 兼容，三家通吃）-----
    provider: str = "aliyun"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""
    vlm_model: str = "qwen-vl-max"
    llm_model: str = "qwen-plus"
    vlm_max_tokens: int = 500
    llm_max_tokens: int = 2000

    # ----- 转写（四级模型选择：free / realtime / pro_lite / pro_pro）-----
    # 凭证定义在 .env（由 pc_api/credentials.py 读取），此处仅记录用户选择的 tier。
    asr_tier: str = "free"
    # 兼容旧字段（pc_local 用本地、安卓可能用 OpenAI 兼容端点），pc_api 不再使用
    transcription_base_url: str = "https://api.openai.com/v1"
    transcription_api_key: str = ""
    transcription_model: str = "whisper-1"

    # ----- 转写语言（zh=中文, en=英文, 空=自动检测）-----
    language: str = "zh"

    # ----- VAD 自动分段(批处理 tier:free/pro_lite/pro_pro 启用,接近实时版体验)-----
    # 说话停顿达阈值即截断当前段,触发独立转写,UI 增量显示。
    # realtime tier 走 WebSocket 流式,不启用 VAD。
    vad_enabled: bool = True
    vad_silence_duration: float = 1.5        # 触发截断的连续静音秒数
    vad_silence_threshold: float = 0.02      # 归一化 RMS(0~1),约 -34dB
    vad_min_segment: float = 1.0            # 短于此丢弃(噪音过滤)
    vad_max_segment: float = 270.0          # 强制截断上限(pro_pro 5min 留 30s 余量)

    # ----- 会议模式(视频会议场景:系统音频 + 截屏 + 悬浮窗)-----
    # 会议模式与 ASR tier 正交:只管采集源,tier 管转写质量。
    meeting_mode: bool = False                          # 是否启用会议模式
    meeting_audio_source: str = "mixed"                 # system / mixed / mic(默认 mixed)
    meeting_screenshot_mode: str = "full"               # full / region(默认 full)
    meeting_auto_float: bool = True                     # 开始录音时自动收起为悬浮窗
    meeting_hotkey: str = "ctrl+alt+s"                  # 截屏全局快捷键
    meeting_screen_index: int = 0                       # 截屏显示器序号(0=主屏)
    # 会议模式系统音频 VAD 阈值:系统音频电平特性与麦克风不同(静音时可能恒为0、
    # 有声音时信号爆表),复用麦克风阈值会导致乱切/不切。需用真实会议音频标定。
    meeting_vad_silence_threshold: float = 0.01         # 归一化 RMS(0~1),默认比 mic 略低

    # ----- UI 主题 -----
    theme: str = "light"    # "light" 或 "dark"
    onboarding_done: bool = False  # 是否已完成首启引导

    def is_vlm_configured(self) -> bool:
        """VLM/LLM 是否配置完整。"""
        return bool(self.api_key and self.base_url and self.vlm_model)

    def is_llm_configured(self) -> bool:
        """LLM 是否配置完整（pc_local VLM 本地时，仅需此项做融合笔记）。"""
        return bool(self.api_key and self.base_url and self.llm_model)

    def is_transcription_configured(self) -> bool:
        """转写是否配置完整。

        pc_api 走四级 tier 选择（asr_tier），凭证硬编码，始终视为已配置；
        pc_local 用本地，无需此项。
        """
        return bool(self.asr_tier)


def _sweep_stale_tmp_files() -> None:
    """清扫进程被硬杀（SIGKILL/断电）残留的原子写临时文件。

    save_user_config 的 except 分支只能清理本进程内异常，硬杀场景下
    .config-*.tmp 会永久残留；每次加载配置时顺手清扫（尽力而为）。
    """
    try:
        for p in CONFIG_DIR.glob(".config-*.tmp"):
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def load_user_config() -> Optional[UserConfig]:
    """从 ~/.airecorder/config.json 加载；不存在返回 None。

    配置损坏时记 warning 日志后同样返回 None——调用方无法避免地走
    首启引导，但至少日志留痕，用户可据此发现 api_key 等配置已丢失。
    """
    if not CONFIG_FILE.exists():
        return None
    _sweep_stale_tmp_files()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"用户配置解析失败（将重新走首启引导）: {CONFIG_FILE} — {e}")
        return None
    try:
        valid_keys = UserConfig.__dataclass_fields__.keys()
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return UserConfig(**filtered)
    except Exception as e:
        # 字段类型与 dataclass 不匹配（如 vlm_max_tokens 存成字符串）
        logger.warning(f"用户配置字段校验失败（将重新走首启引导）: {e}")
        return None


def save_user_config(cfg: UserConfig) -> None:
    """保存到 ~/.airecorder/config.json。

    原子写：先写临时文件再 os.replace，进程崩溃/断电不会留下半截 JSON；
    文件含明文 api_key，尽力收紧权限到 0o600（Windows 上 chmod 仅只读位生效，
    失败静默忽略）。
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(cfg), ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(CONFIG_DIR), prefix=".config-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, CONFIG_FILE)
    except Exception:
        # 清理残留临时文件
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def apply_preset(cfg: UserConfig, provider: str) -> UserConfig:
    """应用厂商预设（base_url + VLM/LLM 模型），key 不覆盖。"""
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["custom"])
    cfg.provider = provider
    cfg.base_url = preset["base_url"]
    cfg.vlm_model = preset["vlm_model"]
    cfg.llm_model = preset["llm_model"]
    return cfg
