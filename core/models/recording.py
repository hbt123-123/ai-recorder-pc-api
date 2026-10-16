"""Recording 与 TranscriptChunk 数据模型（精简版）。

改造自 speakr/src/models/recording.py：
- 从 Flask-SQLAlchemy (db.Model / db.Column / db.session) 迁移到原生
  SQLAlchemy 2.0 (Base 子类 / sqlalchemy.Column)。
- 移除服务器特有字段：user_id, folder_id, is_inbox, is_highlighted,
  file_hash, speaker_embeddings, prompt_variables, keep_audio_only,
  audio_deleted_at, deletion_exempt, resolved_hotwords, resolved_initial_prompt,
  participants, notes, meeting_date, processing_source 等。
- 移除所有跨模型方法（get_visible_tags / get_user_notes / to_dict /
  to_list_dict 等涉及 sharing/organization/folder/tag 的方法）。
- 移除 `from src.utils import md_to_html` 依赖。
- 保留时间轴字段（start_time / end_time）供照片对齐使用。
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, Text, Float, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship

from core.models import Base


class Recording(Base):
    """录音主模型：存储音频文件 locator、转写文本、状态等。"""

    __tablename__ = "recordings"

    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=True)
    audio_path = Column(String(500))  # StorageService locator
    transcription = Column(Text, nullable=True)  # 完整转写文本（或 to_storage_format JSON）
    summary = Column(Text, nullable=True)  # 摘要 / 融合笔记
    status = Column(String(50), default="PENDING")  # PENDING/PROCESSING/COMPLETED/FAILED
    audio_duration_seconds = Column(Float, nullable=True)
    mime_type = Column(String(100), nullable=True)
    original_filename = Column(String(500), nullable=True)
    audio_source = Column(String(20), nullable=True)  # mic / system / mixed（会议模式新增）
    meeting_mode = Column(Boolean, default=False)  # 是否会议模式录音（会议模式新增）
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    chunks = relationship(
        "TranscriptChunk",
        back_populates="recording",
        cascade="all, delete-orphan",
        order_by="TranscriptChunk.chunk_index",
    )
    attachments = relationship(
        "RecordingAttachment",
        back_populates="recording",
        cascade="all, delete-orphan",
        order_by="RecordingAttachment.timestamp",
    )
    audio_segments = relationship(
        "RecordingAudioSegment",
        back_populates="recording",
        cascade="all, delete-orphan",
        order_by="RecordingAudioSegment.segment_index",
    )

    def __repr__(self) -> str:
        return f"<Recording id={self.id} title={self.title!r} status={self.status}>"


class TranscriptChunk(Base):
    """转写片段：带时间戳的文本段，用于照片时间轴对齐与检索。"""

    __tablename__ = "transcript_chunks"

    id = Column(Integer, primary_key=True)
    recording_id = Column(Integer, ForeignKey("recordings.id"), nullable=False)
    chunk_index = Column(Integer, nullable=False)  # 录音内顺序
    content = Column(Text, nullable=False)
    start_time = Column(Float, nullable=True)  # 秒，用于照片时间轴对齐
    end_time = Column(Float, nullable=True)
    speaker_name = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    recording = relationship("Recording", back_populates="chunks")

    def __repr__(self) -> str:
        return f"<TranscriptChunk id={self.id} idx={self.chunk_index} [{self.start_time}-{self.end_time}]>"


class RecordingAudioSegment(Base):
    """录音音频段:VAD 切分出的单段音频文件 + 段级转写状态。

    一次录音(Recording)按静音切成 N 段,每段独立 wav + 独立转写。
    TranscriptChunk 按段内句粒度再细分(由连接器返回的 segments 决定),
    chunk 时间戳在 save_segment_transcription 内已加 seg.start_time 偏移。
    """

    __tablename__ = "recording_audio_segments"

    id = Column(Integer, primary_key=True)
    recording_id = Column(Integer, ForeignKey("recordings.id"), nullable=False)
    segment_index = Column(Integer, nullable=False)  # 段序号,0-based,跨段连续
    audio_path = Column(String(500))  # StorageService locator(单段 wav)
    start_time = Column(Float, nullable=True)  # 相对录音开始的秒数
    end_time = Column(Float, nullable=True)
    duration = Column(Float, nullable=True)
    transcription_status = Column(String(50), default="PENDING")  # PENDING/PROCESSING/COMPLETED/FAILED
    transcription_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    recording = relationship("Recording", back_populates="audio_segments")

    def __repr__(self) -> str:
        return (f"<RecordingAudioSegment id={self.id} idx={self.segment_index} "
                f"[{self.start_time}-{self.end_time}] status={self.transcription_status}>")
