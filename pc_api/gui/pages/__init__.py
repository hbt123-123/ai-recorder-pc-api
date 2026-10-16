"""各 ASR tier 版本定制页面。

BaseTierPage 承载共享的录音+转写+笔记逻辑；
4 个子类覆盖 _build_header() 实现完全独立的定制头部。
"""

from .base_page import BaseTierPage
from .free_page import FreePage
from .realtime_page import RealtimePage
from .pro_lite_page import ProLitePage
from .pro_pro_page import ProProPage

# tier_id -> 页面类
_PAGE_MAP = {
    "free": FreePage,
    "realtime": RealtimePage,
    "pro_lite": ProLitePage,
    "pro_pro": ProProPage,
}


def make_tier_page(tier_id: str, service, cfg, parent=None) -> BaseTierPage:
    """按 tier_id 工厂创建对应版本定制页面。"""
    cls = _PAGE_MAP.get(tier_id, FreePage)
    return cls(service, cfg, parent=parent)


__all__ = [
    "BaseTierPage",
    "FreePage",
    "RealtimePage",
    "ProLitePage",
    "ProProPage",
    "make_tier_page",
]
