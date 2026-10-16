"""RecordingAttachment 数据模型（照片附件）。

新建：录音过程中拍摄的照片，关联时间轴位置与 VLM 生成的描述。

核心字段：
- storage_locator: StorageService 存储路径（图片本体）
- thumbnail_locator: 缩略图路径
- timestamp: 录音时间轴上的秒数（photo_time - recording_start_time）
- description: VLM 生成的图片描述
- description_status: 描述生成状态
- source: 图片来源（camera 拍照 / screenshot 截屏 / file 用户选择）—— 会议模式新增
- fused_chunk_id: 时间轴对齐到的 TranscriptChunk（见 core.fusion.timeline）
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from core.models import Base


class RecordingAttachment(Base):
    """录音照片附件。"""

    __tablename__ = "recording_attachments"

    id = Column(Integer, primary_key=True)
    recording_id = Column(Integer, ForeignKey("recordings.id"), nullable=False)
    attachment_type = Column(String(20), default="image")  # image | video
    storage_locator = Column(String(500))  # StorageService 存储路径
    thumbnail_locator = Column(String(500), nullable=True)  # 缩略图路径
    timestamp = Column(Float, nullable=False)  # 录音时间轴上的秒数
    description = Column(Text, nullable=True)  # VLM 生成的描述
    description_status = Column(String(20), default="PENDING")  # PENDING/PROCESSING/COMPLETED/FAILED
    source = Column(String(20), default="camera")  # camera / screenshot / file（会议模式新增）
    fused_chunk_id = Column(Integer, ForeignKey("transcript_chunks.id"), nullable=True)  # 时间轴对齐的转写片段
    created_at = Column(DateTime, default=datetime.utcnow)

    recording = relationship("Recording", back_populates="attachments")

    def __repr__(self) -> str:
        return (f"<RecordingAttachment id={self.id} type={self.attachment_type} "
                f"source={self.source} t={self.timestamp}s>")
