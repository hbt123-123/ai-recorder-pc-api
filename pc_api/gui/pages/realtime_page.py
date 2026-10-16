"""实时版页面（tier=realtime）：阿里云百炼 paraformer-realtime-8k-v2。"""

from PySide6.QtWidgets import QWidget

from .base_page import BaseTierPage
from core.user_config import UserConfig
from pc_api.recording_service import RecordingService


class RealtimePage(BaseTierPage):
    """实时版定制页面。"""

    TIER_ID = "realtime"

    def __init__(self, service: RecordingService, cfg: UserConfig, parent=None):
        super().__init__(service, cfg, self.TIER_ID, parent)

    def _build_header(self) -> QWidget:
        return self._make_tier_header(
            accent="realtime",
            subtitle="阿里云百炼 paraformer-realtime-8k-v2 · 实时 WebSocket · 8kHz",
            features=[
                ("", "实时 WebSocket 流式转写"),
                ("", "边说边转 · 8kHz"),
                ("", "0.864 元/小时"),
                ("", "录音时自动转写，停止后可生成融合笔记"),
            ],
        )
