"""录音编排服务层。

封装录音 → 存储 → 转写 → VLM 描述 → 入库的完整流程，隔离 GUI/worker
与 core 的直接交互。每个方法内部创建独立 SQLAlchemy session，线程安全。
"""

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from sqlalchemy import func

from core.audio.recorder import AudioRecorder
from core.audio.sources import AudioSource
from core.models import (
    Recording,
    RecordingAttachment,
    RecordingAudioSegment,
    TranscriptChunk,
    get_session,
)
from core.storage.service import StorageService
from core.transcription.base import TranscriptionResponse

logger = logging.getLogger(__name__)


class RecordingService:
    """应用服务层：编排录音、存储、转写、VLM、入库。"""

    def __init__(self, storage: Optional[StorageService] = None):
        self.storage = storage or StorageService()
        self.recorder = AudioRecorder()
        # 分段转写落库互斥锁：多个 SegmentTranscriptionWorker 并发调用
        # save_segment_transcription 时，max(chunk_index) 读-插与
        # Recording.transcription 读-拼-写都是 check-then-act，
        # 无锁会产生重复 chunk_index 与文本丢失更新（SQLite 不报错，静默损坏）。
        self._segment_write_lock = threading.Lock()

    # ---------- 录音 ----------

    def create_recording(self, title: Optional[str] = None,
                         audio_source: Optional[str] = None,
                         meeting_mode: bool = False) -> Recording:
        """创建一条 PENDING 录音记录。

        Args:
            title: 录音标题；None 自动按时间生成
            audio_source: 音源类型（mic/system/mixed），会议模式落库标记
            meeting_mode: 是否会议模式录音
        """
        session = get_session()
        try:
            rec = Recording(title=title or f"录音 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                            status="PENDING",
                            audio_source=audio_source,
                            meeting_mode=meeting_mode)
            session.add(rec)
            session.commit()
            session.refresh(rec)
            # detach 供调用方使用
            session.expunge(rec)
            return rec
        finally:
            session.close()

    def start_recording(self, output_path: Optional[str] = None,
                        source: str = AudioSource.MIC) -> str:
        """开始录音。返回 WAV 输出路径。

        Args:
            output_path: WAV 输出路径；None 则自动生成到 staging 目录
            source: 音源类型，AudioSource.MIC / SYSTEM / MIXED（默认 MIC）
        """
        if output_path is None:
            staging = self.storage.get_staging_dir()
            output_path = os.path.join(staging, f"rec_{int(time.time())}.wav")
        self.recorder.start_recording(output_path, source=source)
        return output_path

    def stop_recording(self) -> Optional[float]:
        """停止录音，返回时长（秒）。"""
        return self.recorder.stop_recording()

    def pause_recording(self) -> None:
        """暂停录音（不停止音频流，仅丢弃帧）。"""
        self.recorder.pause()

    def resume_recording(self) -> None:
        """恢复录音。"""
        self.recorder.resume()

    def store_audio(self, recording_id: int, audio_path: str) -> str:
        """将录音文件存入 StorageService，更新 Recording.audio_path。"""
        key = self.storage.build_recording_key(audio_path, recording_id)
        stored = self.storage.upload_local_file(audio_path, key)
        session = get_session()
        try:
            rec = session.get(Recording, recording_id)
            if rec:
                rec.audio_path = stored.locator
                session.commit()
        finally:
            session.close()
        return stored.locator

    # ---------- 拍照 / 附件 ----------

    def add_attachment(self, recording_id: int, image_path: str,
                       timestamp: Optional[float] = None,
                       source: str = "camera") -> RecordingAttachment:
        """将一张图片存为录音附件。

        Args:
            recording_id: 关联录音 id
            image_path: 图片本地路径
            timestamp: 录音时间轴秒数；None 则用当前录音已过秒数
            source: 图片来源（camera 拍照 / screenshot 截屏 / file 用户选择）
        """
        if timestamp is None:
            timestamp = self.recorder.photo_timestamp() if self.recorder.is_recording else 0.0

        key = self.storage.build_recording_key(image_path, recording_id)
        stored = self.storage.upload_local_file(image_path, key, content_type="image/jpeg")

        session = get_session()
        try:
            att = RecordingAttachment(
                recording_id=recording_id,
                attachment_type="image",
                storage_locator=stored.locator,
                timestamp=timestamp,
                description_status="PENDING",
                source=source,
            )
            session.add(att)
            session.commit()
            session.refresh(att)
            session.expunge(att)
            return att
        finally:
            session.close()

    # ---------- 转写结果 ----------

    def save_transcription(self, recording_id: int, response: TranscriptionResponse,
                           duration: Optional[float] = None) -> None:
        """保存转写结果：纯文本入 Recording.transcription，segments 入 TranscriptChunk。

        与分段追加路径共用 _segment_write_lock：整文件重转写会清空 chunks 并
        覆盖 transcription，若与并发段写入交错会产生半覆盖状态。
        """
        with self._segment_write_lock:
            session = get_session()
            try:
                rec = session.get(Recording, recording_id)
                if not rec:
                    raise ValueError(f"Recording {recording_id} not found")
                rec.transcription = response.text
                rec.status = "COMPLETED"
                if duration is not None:
                    rec.audio_duration_seconds = duration
                rec.completed_at = datetime.utcnow()

                # 清除旧 chunks（重新转写时）
                session.query(TranscriptChunk).filter_by(recording_id=recording_id).delete()

                if response.segments:
                    for i, seg in enumerate(response.segments):
                        session.add(TranscriptChunk(
                            recording_id=recording_id,
                            chunk_index=i,
                            content=seg.text,
                            start_time=seg.start_time,
                            end_time=seg.end_time,
                            speaker_name=seg.speaker,
                        ))
                else:
                    # 无 segments：把整段文本作为单个 chunk，时间戳留空
                    session.add(TranscriptChunk(
                        recording_id=recording_id,
                        chunk_index=0,
                        content=response.text,
                        start_time=None,
                        end_time=None,
                        speaker_name=None,
                    ))
                session.commit()
            finally:
                session.close()

    def set_recording_status(self, recording_id: int, status: str,
                             error: Optional[str] = None) -> None:
        session = get_session()
        try:
            rec = session.get(Recording, recording_id)
            if rec:
                rec.status = status
                if error is not None:
                    rec.error_message = error
                session.commit()
        finally:
            session.close()

    # ---------- VLM 描述 ----------

    def save_attachment_description(self, attachment_id: int, description: str) -> None:
        session = get_session()
        try:
            att = session.get(RecordingAttachment, attachment_id)
            if att:
                att.description = description
                att.description_status = "COMPLETED"
                session.commit()
        finally:
            session.close()

    def set_attachment_status(self, attachment_id: int, status: str) -> None:
        session = get_session()
        try:
            att = session.get(RecordingAttachment, attachment_id)
            if att:
                att.description_status = status
                session.commit()
        finally:
            session.close()

    # ---------- 查询 ----------

    def get_recording(self, recording_id: int) -> Optional[Recording]:
        session = get_session()
        try:
            rec = session.get(Recording, recording_id)
            if rec:
                session.expunge(rec)
            return rec
        finally:
            session.close()

    def list_recordings(self) -> List[Recording]:
        session = get_session()
        try:
            recs = session.query(Recording).order_by(Recording.created_at.desc()).all()
            for r in recs:
                session.expunge(r)
            return recs
        finally:
            session.close()

    def list_attachments(self, recording_id: int) -> List[RecordingAttachment]:
        session = get_session()
        try:
            atts = (session.query(RecordingAttachment)
                    .filter_by(recording_id=recording_id)
                    .order_by(RecordingAttachment.timestamp)
                    .all())
            for a in atts:
                session.expunge(a)
            return atts
        finally:
            session.close()

    def list_chunks(self, recording_id: int) -> List[TranscriptChunk]:
        session = get_session()
        try:
            chunks = (session.query(TranscriptChunk)
                      .filter_by(recording_id=recording_id)
                      .order_by(TranscriptChunk.chunk_index)
                      .all())
            for c in chunks:
                session.expunge(c)
            return chunks
        finally:
            session.close()

    # ---------- 文件解析 ----------

    def resolve_local_path(self, locator: str) -> str:
        """把 storage locator 解析为本地文件系统路径。"""
        return self.storage.resolve_local_filesystem_path(locator)

    def materialize_audio(self, recording_id: int):
        """物化录音音频文件，返回 contextmanager（用于转写时读取）。"""
        session = get_session()
        try:
            rec = session.get(Recording, recording_id)
            locator = rec.audio_path if rec else None
        finally:
            session.close()
        if not locator:
            raise ValueError(f"Recording {recording_id} has no audio_path")
        return self.storage.materialize(locator)

    # ---------- 分段录音(VAD 自动分段模式) ----------

    def start_segmented_recording(self, recording_id: int,
                                  output_dir: Optional[str] = None,
                                  source: str = AudioSource.MIC) -> str:
        """启动分段录音模式,返回 output_dir。

        与 ``start_recording`` 互斥。在内部调用 AudioRecorder
        ``start_recording_segmented``,启动第 0 段 WAV。

        Args:
            recording_id: 录音 id,用于段文件命名
            output_dir: 段 wav 输出目录;None 则用 staging 目录
            source: 音源类型,AudioSource.MIC / SYSTEM / MIXED(默认 MIC)
        """
        if output_dir is None:
            output_dir = self.storage.get_staging_dir()
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        self.recorder.start_recording_segmented(output_dir, recording_id, source=source)
        return output_dir

    def add_audio_segment(self, recording_id: int, segment_index: int,
                          local_audio_path: str, start_time: float,
                          end_time: float, duration: float) -> int:
        """落库一段音频文件,返回 segment id。

        把本地 wav 上传到 StorageService,创建 RecordingAudioSegment 记录。
        """
        # 用 filename 带段号区分(StorageService 自动按 recording_id 分目录)
        seg_filename = f"rec_{recording_id}_seg{segment_index}.wav"
        key = self.storage.build_recording_key(seg_filename, recording_id)
        stored = self.storage.upload_local_file(
            local_audio_path, key, content_type="audio/wav"
        )
        session = get_session()
        try:
            seg = RecordingAudioSegment(
                recording_id=recording_id,
                segment_index=segment_index,
                audio_path=stored.locator,
                start_time=start_time,
                end_time=end_time,
                duration=duration,
                transcription_status="PENDING",
            )
            session.add(seg)
            session.commit()
            session.refresh(seg)
            session.expunge(seg)
            logger.info(
                f"段 {segment_index} 入库: rec={recording_id} "
                f"[{start_time:.1f}-{end_time:.1f}s] dur={duration:.1f}s "
                f"locator={stored.locator}"
            )
            return seg.id
        finally:
            session.close()

    def set_segment_status(self, segment_id: int, status: str,
                            error: Optional[str] = None) -> None:
        """更新段转写状态。"""
        session = get_session()
        try:
            seg = session.get(RecordingAudioSegment, segment_id)
            if seg:
                seg.transcription_status = status
                if error is not None:
                    seg.transcription_error = error
                if status == "COMPLETED":
                    seg.completed_at = datetime.utcnow()
                session.commit()
        finally:
            session.close()

    def save_segment_transcription(self, recording_id: int, segment_id: int,
                                    response: TranscriptionResponse) -> int:
        """把单段转写结果追加为 TranscriptChunk(全局 chunk_index 递增)。

        段内 segment 时间戳是相对段起始,需 + seg.start_time 转为相对录音。
        同时累积 Recording.transcription 文本。返回新增 chunk 数量。

        并发安全：方法体在 ``_segment_write_lock`` 内执行，串行化
        max(chunk_index)+插入与 transcription 累积的复合写。
        """
        with self._segment_write_lock:
            session = get_session()
            try:
                seg = session.get(RecordingAudioSegment, segment_id)
                if not seg:
                    raise ValueError(f"Segment {segment_id} not found")
                seg.transcription_status = "COMPLETED"
                seg.completed_at = datetime.utcnow()
                if seg.transcription_error:
                    seg.transcription_error = None

                seg_offset = seg.start_time or 0.0

                # 取当前最大 chunk_index,新 chunk 从 +1 开始
                max_idx = (session.query(func.max(TranscriptChunk.chunk_index))
                           .filter_by(recording_id=recording_id).scalar())
                # 注意不能写 (max_idx or -1)：chunk_index=0 是合法最大值，
                # or 会把它误判为 falsy 导致新块复用索引 0
                next_idx = (max_idx if max_idx is not None else -1) + 1

                added = 0
                if response.segments:
                    for s in response.segments:
                        if not s.text or not s.text.strip():
                            continue
                        session.add(TranscriptChunk(
                            recording_id=recording_id,
                            chunk_index=next_idx + added,
                            content=s.text,
                            start_time=(s.start_time + seg_offset)
                                       if s.start_time is not None else None,
                            end_time=(s.end_time + seg_offset)
                                     if s.end_time is not None else None,
                            speaker_name=s.speaker,
                        ))
                        added += 1
                elif response.text and response.text.strip():
                    session.add(TranscriptChunk(
                        recording_id=recording_id,
                        chunk_index=next_idx,
                        content=response.text,
                        start_time=seg.start_time,
                        end_time=seg.end_time,
                    ))
                    added = 1

                # 累积 Recording.transcription 文本(供笔记融合用)
                rec = session.get(Recording, recording_id)
                if rec:
                    existing = rec.transcription or ""
                    rec.transcription = existing + (response.text or "")
                session.commit()
                logger.info(
                    f"段 {seg.segment_index} 转写落库: +{added} chunks, "
                    f"rec={recording_id} text_len={len(response.text or '')}"
                )
                return added
            finally:
                session.close()

    def get_segment_start_time(self, segment_id: int) -> Optional[float]:
        """获取段起始时间(相对录音开始的秒数),供 worker 给段内时间戳加偏移。"""
        session = get_session()
        try:
            seg = session.get(RecordingAudioSegment, segment_id)
            return seg.start_time if seg else None
        finally:
            session.close()

    def finalize_segmented_recording(self, recording_id: int,
                                      total_duration: float) -> None:
        """所有段转写完成,标记 Recording COMPLETED。

        与 ``save_transcription`` 不同,此处不覆盖 chunks(段已逐个追加)。
        """
        session = get_session()
        try:
            rec = session.get(Recording, recording_id)
            if rec:
                rec.status = "COMPLETED"
                rec.audio_duration_seconds = total_duration
                rec.completed_at = datetime.utcnow()
                session.commit()
                logger.info(
                    f"分段录音收尾: rec={recording_id} dur={total_duration:.1f}s"
                )
        finally:
            session.close()

    def list_audio_segments(self, recording_id: int) -> List[RecordingAudioSegment]:
        """列出录音的所有段(按 segment_index 顺序)。"""
        session = get_session()
        try:
            segs = (session.query(RecordingAudioSegment)
                    .filter_by(recording_id=recording_id)
                    .order_by(RecordingAudioSegment.segment_index)
                    .all())
            for s in segs:
                session.expunge(s)
            return segs
        finally:
            session.close()

    # ---------- 会话管理(重命名 / 删除 / 搜索) ----------

    def rename_recording(self, recording_id: int, new_title: str) -> None:
        """重命名录音标题。

        空字符串/纯空白视作无效,直接返回保留原标题(避免误清空)。
        标题按 title 字段长度上限 200 字截断(与 DB schema 对齐)。
        """
        new_title = (new_title or "").strip()[:200]
        if not new_title:
            return
        session = get_session()
        try:
            rec = session.get(Recording, recording_id)
            if rec:
                rec.title = new_title
                session.commit()
                logger.info(f"重命名录音: id={recording_id} -> {new_title!r}")
        finally:
            session.close()

    def delete_recording(self, recording_id: int) -> None:
        """删除录音 + 级联清理 DB 行 + 尽力清理 storage 文件。

        DB 级联: SQLAlchemy relationship 已声明 cascade='all, delete-orphan',
        删除 Recording 自动级联删 chunks / attachments / audio_segments。
        storage 文件: 先收集所有 locator 列表,DB commit 后再依次 os.remove;
        文件删除失败仅 warn,不影响 DB 一致性(磁盘残留可由清理任务回收)。
        """
        session = get_session()
        locators: List[str] = []
        try:
            rec = session.get(Recording, recording_id)
            if not rec:
                logger.info(f"delete_recording: id={recording_id} 不存在,跳过")
                return
            # 收集所有需要清理的 storage locator
            if rec.audio_path:
                locators.append(rec.audio_path)
            for a in rec.attachments:
                if a.storage_locator:
                    locators.append(a.storage_locator)
            for s in rec.audio_segments:
                if s.audio_path:
                    locators.append(s.audio_path)
            # DB 级联删除
            session.delete(rec)
            session.commit()
            logger.info(
                f"删除录音: id={recording_id} 关联 {len(locators)} 个 storage 文件"
            )
        finally:
            session.close()
        # DB 提交后清理 storage 文件(失败不回滚)
        for loc in locators:
            try:
                path = self.storage.resolve_local_filesystem_path(loc)
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                logger.warning(f"删除 storage 文件失败 {loc}: {e}")

    def search_recordings(self, keyword: str) -> List[Recording]:
        """按标题/转写内容搜索;空关键字返回全部。

        SQLite ILIKE 在 SQLAlchemy 内部会被翻译为 LIKE,大小写不敏感。
        transcription 字段可能很长,ILIKE 性能可接受(数据量级为百条级)。
        """
        if not keyword or not keyword.strip():
            return self.list_recordings()
        kw = f"%{keyword.strip()}%"
        session = get_session()
        try:
            from sqlalchemy import or_
            recs = (session.query(Recording)
                    .filter(or_(Recording.title.ilike(kw),
                                Recording.transcription.ilike(kw)))
                    .order_by(Recording.created_at.desc()).all())
            for r in recs:
                session.expunge(r)
            return recs
        finally:
            session.close()
