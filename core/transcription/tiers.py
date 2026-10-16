"""三级（四档）ASR 模型选择 Tier 注册表。

集中定义每档的元数据，供 UI 展示与 config_loader 映射共用。
连接器实现与凭证见各 connector 模块与 pc_api/credentials.py。
"""

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class TierDef:
    """单档转写模型定义。"""

    tier_id: str        # 唯一标识
    label: str          # 显示名（如"免费版"）
    connector: str      # 对应 registry 中的连接器名
    model: str          # 模型名
    price: str          # 价格展示
    description: str    # 厂商 / 模型描述


# 四档 tier 定义
TIER_DEFS: Dict[str, TierDef] = {
    "free": TierDef(
        tier_id="free",
        label="免费版",
        connector="openai_whisper",
        model="FunAudioLLM/SenseVoiceSmall",
        price="免费",
        description="硅基流动 FunAudioLLM/SenseVoiceSmall · OpenAI 兼容端点 · 单文件 ≤1 小时 / 50MB",
    ),
    "realtime": TierDef(
        tier_id="realtime",
        label="实时版",
        connector="dashscope_realtime",
        model="paraformer-realtime-8k-v2",
        price="0.864 元/小时",
        description="阿里云百炼 paraformer-realtime-8k-v2 · 实时 WebSocket · 8kHz",
    ),
    "pro_lite": TierDef(
        tier_id="pro_lite",
        label="专业版 Lite",
        connector="volc_doubao_file",
        model="bigmodel",
        price="0.77 元/小时",
        description="火山引擎 豆包录音文件识别模型 2.0 · 文件识别（submit+query）",
    ),
    "pro_pro": TierDef(
        tier_id="pro_pro",
        label="专业版 Pro",
        connector="dashscope_funasr",
        model="fun-asr-flash-2026-06-15",  # 已核实：阿里云百炼真实模型 ID
        price="0.792 元/小时",
        description="阿里云百炼 fun-asr-flash · 同步录音文件识别 REST（multimodal-generation 端点 · base64 内联 · ≤5min）",
    ),
}

# UI 下拉顺序
TIER_ORDER: List[str] = ["free", "realtime", "pro_lite", "pro_pro"]

DEFAULT_TIER = "free"


def get_tier(tier_id: Optional[str]) -> TierDef:
    """按 id 取 TierDef；未知则回退到 free。"""
    if not tier_id or tier_id not in TIER_DEFS:
        return TIER_DEFS[DEFAULT_TIER]
    return TIER_DEFS[tier_id]


def list_tiers() -> List[TierDef]:
    """按 TIER_ORDER 顺序返回所有 TierDef（供 UI 渲染）。"""
    return [TIER_DEFS[t] for t in TIER_ORDER]
