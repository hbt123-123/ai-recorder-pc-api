"""专业版 Lite 页面（tier=pro_lite）：火山引擎豆包录音文件识别 2.0。"""

from PySide6.QtWidgets import QWidget

from .base_page import BaseTierPage
from core.user_config import UserConfig
from pc_api.recording_service import RecordingService


class ProLitePage(BaseTierPage):
    """专业版 Lite 定制页面。"""

    TIER_ID = "pro_lite"

    def __init__(self, service: RecordingService, cfg: UserConfig, parent=None):
        super().__init__(service, cfg, self.TIER_ID, parent)

    def _build_header(self) -> QWidget:
        return self._make_tier_header(
            accent="pro_lite",
            subtitle="火山引擎 豆包录音文件识别 2.0 · submit+query 异步",
            features=[
                ("", "文件异步识别"),
                ("", "需服务器 SFTP 托管音频"),
                ("", "0.77 元/小时"),
                ("", "长录音 · 性价比优先"),
            ],
        )
