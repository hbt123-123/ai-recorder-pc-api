"""免费版页面（tier=free）：头部文案从 TIER_DEFS 动态渲染。"""

from PySide6.QtWidgets import QWidget

from .base_page import BaseTierPage
from core.transcription.tiers import TIER_DEFS
from core.user_config import UserConfig
from pc_api.recording_service import RecordingService


class FreePage(BaseTierPage):
    """免费版定制页面。"""

    TIER_ID = "free"

    def __init__(self, service: RecordingService, cfg: UserConfig, parent=None):
        super().__init__(service, cfg, self.TIER_ID, parent)

    def _build_header(self) -> QWidget:
        tier = TIER_DEFS[self.TIER_ID]
        return self._make_tier_header(
            accent="free",
            subtitle=tier.description,
            features=[
                ("", f"模型 {tier.model}"),
                ("", f"计费 {tier.price}"),
                ("", "OpenAI 兼容端点"),
                ("", "适合短录音试听"),
            ],
        )
