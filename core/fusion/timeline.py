"""时间轴对齐：照片 timestamp ↔ TranscriptChunk。

对齐规则（文档 7.1）：
- 录音开始记录 recording_start_time = time.time()
- 拍照时 attachment.timestamp = photo_time - recording_start_time
- 转写 segment 带 start_time/end_time（秒），存入 TranscriptChunk
- attachment.timestamp 落入 [chunk.start_time, chunk.end_time] 即关联

融合笔记生成（note_fuser）在阶段 4 实现。
"""

from typing import Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from core.models import TranscriptChunk, RecordingAttachment


def align_attachment_to_chunk(
    session: Session,
    attachment: RecordingAttachment,
) -> Optional[TranscriptChunk]:
    """将单个附件对齐到时间轴上的转写片段。

    匹配：attachment.timestamp 落入 [chunk.start_time, chunk.end_time]。
    命中则写入 attachment.fused_chunk_id 并返回该 chunk；否则返回 None。

    Args:
        session: SQLAlchemy session
        attachment: 待对齐的照片附件

    Returns:
        命中的 TranscriptChunk 或 None
    """
    chunk = (
        session.query(TranscriptChunk)
        .filter(
            TranscriptChunk.recording_id == attachment.recording_id,
            TranscriptChunk.start_time <= attachment.timestamp,
            TranscriptChunk.end_time >= attachment.timestamp,
        )
        .order_by(TranscriptChunk.chunk_index)
        .first()
    )
    if chunk:
        attachment.fused_chunk_id = chunk.id
    return chunk


def align_attachments_for_recording(session: Session, recording_id: int) -> int:
    """对齐某录音下所有未对齐的附件。返回对齐成功的数量。"""
    attachments = (
        session.query(RecordingAttachment)
        .filter(
            RecordingAttachment.recording_id == recording_id,
            RecordingAttachment.fused_chunk_id.is_(None),
        )
        .all()
    )
    count = 0
    for att in attachments:
        if align_attachment_to_chunk(session, att):
            count += 1
    return count


def get_context_text_for_attachment(
    session: Session,
    attachment: RecordingAttachment,
    window_seconds: float = 30.0,
) -> str:
    """获取附件时间轴附近的转写文本，供 VLM 融合使用。

    返回 [timestamp - window, timestamp + window] 区间内转写片段的拼接文本，
    每段带时间戳与说话人标注。

    Args:
        session: SQLAlchemy session
        attachment: 照片附件
        window_seconds: 时间窗口半径（秒）

    Returns:
        拼接的转写文本；无匹配则返回空字符串
    """
    t = attachment.timestamp
    chunks = (
        session.query(TranscriptChunk)
        .filter(
            TranscriptChunk.recording_id == attachment.recording_id,
            # 两类命中：① 片段起点落在窗口内；② 长片段（VAD 段可达数分钟）
            # 起点在窗口之前但时间跨度覆盖附件时刻 —— 漏掉它会让照片
            # 发生时正在讨论的内容进不了 VLM 融合上下文。
            or_(
                and_(
                    TranscriptChunk.start_time >= t - window_seconds,
                    TranscriptChunk.start_time <= t + window_seconds,
                ),
                and_(
                    TranscriptChunk.start_time <= t,
                    or_(
                        TranscriptChunk.end_time.is_(None),
                        TranscriptChunk.end_time >= t,
                    ),
                ),
            ),
        )
        .order_by(TranscriptChunk.chunk_index)
        .all()
    )
    parts = []
    for c in chunks:
        speaker = f"[{c.speaker_name}] " if c.speaker_name else ""
        ts = f"({c.start_time:.1f}s) " if c.start_time is not None else ""
        parts.append(f"{ts}{speaker}{c.content}")
    return "\n".join(parts)
