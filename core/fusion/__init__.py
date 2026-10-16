"""多模态融合：照片 ↔ 转写时间轴对齐、图文融合笔记。"""

from .timeline import (
    align_attachment_to_chunk,
    align_attachments_for_recording,
    get_context_text_for_attachment,
)
from .note_fuser import NoteFuser

__all__ = [
    "align_attachment_to_chunk",
    "align_attachments_for_recording",
    "get_context_text_for_attachment",
    "NoteFuser",
]
