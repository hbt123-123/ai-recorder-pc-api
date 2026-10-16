"""音频文件上传转写后台任务(QThread)。

用户上传音频文件 -> 读取+重采样到 16kHz mono -> VAD 分段切割 -> 逐段转写
-> 增量显示。与实时录音的 VAD 分段流程类似,但音频源是文件而非麦克风实时流。

支持两种模式:
- VAD 启用(free/pro_lite/pro_pro): 读文件 -> VAD 切割 -> 逐段转写 -> 增量显示
- VAD 禁用或 realtime tier: 整文件直接转写

设计要点:
- 复用 VADSegmenter 状态机,分块喂帧模拟实时音频流
- 复用 SegmentTranscriptionWorker 的落库逻辑(service.add_audio_segment +
  service.save_segment_transcription),保证 DB 数据结构与实时录音一致
- 段 WAV 写入 staging 目录,转写后清理临时文件
- soundfile 读取音频(支持 WAV/FLAC/OGG 等 libsndfile 格式),自动重采样
"""

import logging
import os
import wave
from typing import List, Optional, Tuple

import numpy as np
from PySide6.QtCore import QThread, Signal

from core.audio.vad_segmenter import VADSegmenter
from core.transcription import get_connector, TranscriptionRequest
from core.transcription.base import TranscriptionResponse
from core.transcription.exceptions import TranscriptionError, ConfigurationError
from core.user_config import UserConfig
from pc_api.recording_service import RecordingService

logger = logging.getLogger(__name__)

# 与 AudioRecorder 一致的采样率
_FILE_SAMPLE_RATE = 16000
# VAD 喂帧块大小(与实时录音的 sounddevice 块大小近似)
_CHUNK_FRAMES = 1024


class FileTranscriptionWorker(QThread):
    """音频文件上传转写 worker。

    信号:
        progress(str): 进度消息
        segment_done(int, int, list): (recording_id, segment_index,
            [(text, start_s, end_s), ...]) 单段转写完成,供 UI 增量显示
        all_done(int, str): (recording_id, transcription_text) 全部完成
        failed(int, str): (recording_id, error_message)
    """

    progress = Signal(str)
    segment_done = Signal(int, int, list)
    all_done = Signal(int, str)
    failed = Signal(int, str)

    def __init__(self, service: RecordingService, recording_id: int,
                 file_path: str, cfg: UserConfig, tier_id: str,
                 parent=None):
        super().__init__(parent)
        self.service = service
        self.recording_id = recording_id
        self.file_path = file_path
        self.cfg = cfg
        self.tier_id = tier_id

    def run(self) -> None:
        rid = self.recording_id
        try:
            # 先把原文件拷入 storage（大文件 shutil.copy2 放 worker 线程，
            # 避免阻塞 GUI 事件循环），失败则整单失败
            self.progress.emit("正在存储音频文件…")
            try:
                self.service.store_audio(rid, self.file_path)
            except Exception as e:
                logger.exception(f"存储上传音频失败: rid={rid}")
                self.failed.emit(rid, f"存储音频文件失败：{e}")
                try:
                    self.service.set_recording_status(rid, "FAILED", error=str(e))
                except Exception:
                    logger.exception("录音状态落库失败")
                return

            self.progress.emit("正在读取音频文件…")

            # 读取音频文件并重采样到 16kHz mono int16
            audio_data = self._read_and_resample(self.file_path)
            if audio_data is None or len(audio_data) == 0:
                self.failed.emit(rid, "音频文件为空或无法读取")
                return

            duration = len(audio_data) / _FILE_SAMPLE_RATE
            self.progress.emit(f"音频时长 {duration:.1f}s,开始处理…")
            logger.info(
                f"文件转写: rid={rid} file={self.file_path} "
                f"duration={duration:.1f}s samples={len(audio_data)}"
            )

            # 判断是否走 VAD 分段(与 BaseTierPage._is_vad_segmented 逻辑一致)
            use_vad = (
                self.tier_id != "realtime"
                and bool(getattr(self.cfg, "vad_enabled", True))
            )

            if use_vad:
                self._transcribe_with_vad(rid, audio_data, duration)
            else:
                self._transcribe_whole(rid, audio_data, duration)

        except (ConfigurationError, TranscriptionError) as e:
            logger.error(f"文件转写失败: {e}")
            # 先发终态信号再落库状态：DB 写失败时 GUI 仍能收到失败通知
            self.failed.emit(rid, str(e))
            try:
                self.service.set_recording_status(rid, "FAILED", error=str(e))
            except Exception:
                logger.exception("录音状态落库失败")
        except Exception as e:
            logger.exception("文件转写异常")
            self.failed.emit(rid, str(e))
            try:
                self.service.set_recording_status(rid, "FAILED", error=str(e))
            except Exception:
                logger.exception("录音状态落库失败")

    # ---- 音频读取 ----

    def _read_and_resample(self, file_path: str) -> Optional[np.ndarray]:
        """读取音频文件,重采样到 16kHz mono int16。

        使用 soundfile 读取(支持 WAV/FLAC/OGG 等 libsndfile 格式)。
        若原始采样率 != 16kHz,用线性插值重采样。
        直接以 float32 读取并沿声道均值下混,避免 int16 中间拷贝,
        长音频(小时级)可显著降低内存峰值。
        """
        import soundfile as sf

        data, sr = sf.read(file_path, dtype="float32", always_2d=True)

        # 多声道 -> mono(shape (n, ch) -> (n,))
        if data.shape[1] > 1:
            mono = data.mean(axis=1)
        else:
            mono = data[:, 0]

        # 重采样到 16kHz
        if sr != _FILE_SAMPLE_RATE:
            num_samples = int(len(mono) * _FILE_SAMPLE_RATE / sr)
            if num_samples < 1:
                return np.zeros(0, dtype=np.int16)
            indices = np.linspace(0, len(mono) - 1, num_samples)
            mono = np.interp(indices, np.arange(len(mono)), mono)

        # float32 归一化 [-1,1] → int16：先缩放到满幅再 round+clip。
        # 顺序不能反——clip 施加在未缩放域会把整段音频截成 ±1 LSB 的数字静音，
        # 且对 float 源中合法超出 ±1.0 的采样无法正确削波。
        return np.clip(np.round(mono * 32767.0), -32768, 32767).astype(np.int16)

    # ---- VAD 分段模式 ----

    def _transcribe_with_vad(self, rid: int, audio_data: np.ndarray,
                             duration: float) -> None:
        """VAD 分段模式:读文件 -> VAD 切割 -> 逐段转写 -> 增量显示。"""
        self.progress.emit("VAD 分段中…")
        self.service.set_recording_status(rid, "PROCESSING")

        # 运行 VAD 收集段边界
        segments = self._run_vad(audio_data)
        if not segments:
            # VAD 未检测到有效语音段,回退整段转写
            self.progress.emit("未检测到有效语音段,整文件转写…")
            self._transcribe_whole(rid, audio_data, duration)
            return

        self.progress.emit(f"检测到 {len(segments)} 段,开始逐段转写…")
        logger.info(f"VAD 切割: {len(segments)} 段, 总时长 {duration:.1f}s")

        staging = self.service.storage.get_staging_dir()
        connector = get_connector()

        for seg_idx, (start_s, end_s) in enumerate(segments):
            # 提取段音频
            start_sample = int(start_s * _FILE_SAMPLE_RATE)
            end_sample = int(end_s * _FILE_SAMPLE_RATE)
            seg_audio = audio_data[start_sample:end_sample]

            # 写段 WAV 到 staging
            seg_wav_path = os.path.join(
                staging, f"rec_{rid}_upload_seg{seg_idx}.wav"
            )
            # 入库/转写/清理整体纳入 try/finally：任何一步失败都要保证
            # 临时段 WAV 被清理，且后续段继续处理
            seg_id: Optional[int] = None
            try:
                self._write_wav(seg_wav_path, seg_audio)

                # 入库段元数据
                seg_duration = end_s - start_s
                seg_id = self.service.add_audio_segment(
                    rid, seg_idx, seg_wav_path, start_s, end_s, seg_duration
                )
                self.service.set_segment_status(seg_id, "PROCESSING")

                # 转写该段
                self.progress.emit(f"转写段 {seg_idx}({seg_duration:.1f}s)…")
                with open(seg_wav_path, "rb") as f:
                    request = TranscriptionRequest(
                        audio_file=f,
                        filename=f"rec_seg{seg_idx}.wav",
                        mime_type="audio/wav",
                        extra_options={"local_path": seg_wav_path},
                    )
                    response: TranscriptionResponse = connector.transcribe(request)

                # 落库(段内 chunk_index 全局递增,时间戳加段起始偏移)
                self.service.save_segment_transcription(rid, seg_id, response)
                self.service.set_segment_status(seg_id, "COMPLETED")

                # 构造 chunks_meta(时间戳已加段偏移)
                chunks_meta = self._build_chunks_meta(response, start_s, end_s)
                self.segment_done.emit(rid, seg_idx, chunks_meta)

            except Exception as e:
                logger.error(f"段 {seg_idx} 转写失败: {e}")
                if seg_id is not None:
                    try:
                        self.service.set_segment_status(seg_id, "FAILED", error=str(e))
                    except Exception:
                        logger.exception(f"段 {seg_idx} 状态落库失败")
                self.segment_done.emit(
                    rid, seg_idx,
                    [(f"[段 {seg_idx} 转写失败: {e}]", start_s, end_s)]
                )
            finally:
                # 清理临时段 WAV（含写入/入库阶段失败的路径）
                try:
                    os.remove(seg_wav_path)
                except Exception:
                    pass

        # 收尾
        self.service.finalize_segmented_recording(rid, duration)
        rec = self.service.get_recording(rid)
        text = rec.transcription if rec and rec.transcription else ""
        self.all_done.emit(rid, text)

    def _run_vad(self, audio_data: np.ndarray) -> List[Tuple[float, float]]:
        """对音频数据运行 VAD,返回段边界列表 [(start_s, end_s), ...]。"""
        cfg = self.cfg
        segments: List[Tuple[float, float]] = []

        def on_finalize(seg_idx: int, start_s: float, end_s: float):
            segments.append((start_s, end_s))

        vad = VADSegmenter(
            silence_threshold=cfg.vad_silence_threshold,
            silence_duration=cfg.vad_silence_duration,
            min_segment_seconds=cfg.vad_min_segment,
            max_segment_seconds=cfg.vad_max_segment,
            on_segment_finalize=on_finalize,
            sample_rate=_FILE_SAMPLE_RATE,
        )
        vad.start()

        # 分块喂帧(模拟实时录音的帧回调)
        total = len(audio_data)
        offset = 0
        while offset < total:
            chunk = audio_data[offset:offset + _CHUNK_FRAMES]
            actual_len = len(chunk)
            if actual_len < _CHUNK_FRAMES:
                # 末尾不足一帧,补零到完整帧
                chunk = np.pad(chunk, (0, _CHUNK_FRAMES - actual_len))
            vad.consume_frame(chunk.reshape(-1, 1), _CHUNK_FRAMES)
            offset += _CHUNK_FRAMES

        # 处理最后一段
        last = vad.stop()
        if last is not None:
            _, start_s, end_s = last
            segments.append((start_s, end_s))

        return segments

    # ---- 整文件转写模式 ----

    def _transcribe_whole(self, rid: int, audio_data: np.ndarray,
                          duration: float) -> None:
        """整文件转写模式(无 VAD 或 realtime tier)。"""
        self.progress.emit("整文件转写中…")
        self.service.set_recording_status(rid, "PROCESSING")

        staging = self.service.storage.get_staging_dir()
        wav_path = os.path.join(staging, f"rec_{rid}_upload.wav")
        self._write_wav(wav_path, audio_data)

        connector = get_connector()
        try:
            with open(wav_path, "rb") as f:
                request = TranscriptionRequest(
                    audio_file=f,
                    filename="audio.wav",
                    mime_type="audio/wav",
                    extra_options={"local_path": wav_path},
                )
                response: TranscriptionResponse = connector.transcribe(request)
        finally:
            try:
                os.remove(wav_path)
            except Exception:
                pass

        self.service.save_transcription(rid, response, duration)

        # 构造 chunks_meta 供 UI 显示
        chunks_meta = self._build_chunks_meta(response, 0.0, duration)
        self.segment_done.emit(rid, 0, chunks_meta)
        self.all_done.emit(rid, response.text)

    # ---- 辅助 ----

    @staticmethod
    def _build_chunks_meta(response: TranscriptionResponse,
                           seg_start: float,
                           seg_end: float) -> list:
        """从转写响应构造 UI 显示用的 chunks 元数据列表。

        每项 (text, start_s, end_s),时间戳加段起始偏移。
        """
        chunks_meta = []
        if response.segments:
            for s in response.segments:
                if not s.text or not s.text.strip():
                    continue
                chunks_meta.append((
                    s.text,
                    (s.start_time + seg_start) if s.start_time is not None else seg_start,
                    (s.end_time + seg_start) if s.end_time is not None else None,
                ))
        elif response.text and response.text.strip():
            chunks_meta.append((response.text, seg_start, seg_end))
        return chunks_meta

    @staticmethod
    def _write_wav(path: str, data: np.ndarray) -> None:
        """写 int16 mono 16kHz WAV 文件。"""
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(_FILE_SAMPLE_RATE)
            wf.writeframes(data.tobytes())
