"""专业版 Pro 页面（tier=pro_pro）：阿里云百炼 fun-asr-flash-2026-06-15。"""

from PySide6.QtWidgets import QWidget

from .base_page import BaseTierPage
from core.user_config import UserConfig
from pc_api.recording_service import RecordingService


class ProProPage(BaseTierPage):
    """专业版 Pro 定制页面。"""

    TIER_ID = "pro_pro"

    def __init__(self, service: RecordingService, cfg: UserConfig, parent=None):
        super().__init__(service, cfg, self.TIER_ID, parent)

    def _build_header(self) -> QWidget:
        return self._make_tier_header(
            accent="pro_pro",
            subtitle="阿里云百炼 fun-asr-flash · 同步 multimodal-generation · base64 内联",
            features=[
                ("", "词级时间戳"),
                ("", "同步 ≤5min"),
                ("", "0.792 元/小时"),
                ("", "最高识别质量"),
            ],
        )
