"""音频录制引擎。

支持三种音源（会议模式切换）：麦克风 / 系统音频 loopback / 混音。
所有音源在策略层统一产出 16kHz/16-bit/mono int16 帧，本类负责写 WAV、
算音量、通知订阅者。格式：16kHz, 16-bit, mono（Whisper 标准，全链路依赖）。

时间戳机制：
- start_recording() 记录 recording_start_time = time.time()
- 录音中拍照时，attachment.timestamp = photo_time - recording_start_time
- 转写返回的 segment 带 start_time/end_time（秒），存入 TranscriptChunk
- 对齐见 core.fusion.timeline

音源策略见 core.audio.sources：
- AudioSource.MIC：sounddevice 麦克风（默认，向后兼容）
- AudioSource.SYSTEM：pyaudiowpatch WASAPI loopback（会议对方发言）
- AudioSource.MIXED：两路混音（系统+麦克风）

注意：sounddevice / pyaudiowpatch 均在策略类内延迟导入，避免无硬件/未装
依赖时 `from core.audio.recorder import AudioRecorder` 失败。
"""

import logging
import time
import threading
import wave
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

from core.audio.sources import AudioSource, create_source, _AudioSourceStrategy

logger = logging.getLogger(__name__)
# 注意:本模块未配置 root logger,日志输出依赖上层配置。保留 logger 以便
# rotate_segment / VAD 等扩展模块统一日志输出。


class AudioRecorder:
    """录音器：16kHz / 16-bit / mono WAV，回调式实时写入。

    支持三种音源（会议模式切换）：麦克风 / 系统音频 loopback / 混音。
    音源策略在 core.audio.sources 实现，本类通过策略类开流并统一处理帧。
    """

    SAMPLE_RATE = 16000
    CHANNELS = 1
    SAMPLE_WIDTH = 2  # 16-bit = 2 bytes

    def __init__(self):
        self._stream = None  # 旧字段保留（向后兼容判断），实际由 _source 管理
        self._source: Optional[_AudioSourceStrategy] = None
        self._current_source: str = AudioSource.MIC
        self._wav_file: Optional[wave.Wave_write] = None
        self.recording_start_time: Optional[float] = None
        self._output_path: Optional[str] = None
        self._lock = threading.Lock()
        self._is_recording = False
        self._is_paused = False
        self._pause_start_time: Optional[float] = None
        self._total_pause_duration: float = 0.0
        # 实时音量（0.0~1.0），供声波可视化读取。单个 float 赋值在
        # CPython GIL 下原子，音频线程写、GUI 线程读无需额外锁。
        self._current_level = 0.0
        # 实时音频帧订阅者（如实时转写 worker / VAD 分段器）。独立锁，避免阻塞音频回调。
        self._subscribers = []
        self._sub_lock = threading.Lock()
        # ----- 分段录音模式（VAD 自动分段：批处理 tier 使用）-----
        # _segmented_mode=True 时,按段写独立 wav 文件,rotate_segment() 切换段。
        # realtime tier 与旧 API(单 wav 文件)不受影响。
        self._segmented_mode = False
        self._segment_index = 0
        self._segment_output_dir: Optional[str] = None
        self._segment_recording_id: Optional[int] = None
        self._last_segment_path: Optional[str] = None  # 最近一段关闭后的路径(供收尾读取)
        self._segment_samples = 0  # 当前段已写入样本数(供段时长统计)

    def add_subscriber(self, callback) -> None:
        """注册实时音频帧订阅者。

        callback(indata: np.ndarray, frames: int) -> None，在 sounddevice
        音频线程调用。indata 为 int16、shape (frames, 1)、16kHz mono 只读视图。
        订阅者必须非阻塞、轻量（如仅做重采样 + Queue.put）。
        """
        with self._sub_lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def remove_subscriber(self, callback) -> None:
        with self._sub_lock:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

    def start_recording(self, output_path: str,
                        source: str = AudioSource.MIC,
                        **source_kwargs) -> float:
        """开始录音，写入 output_path（WAV）。

        Args:
            output_path: 输出 WAV 文件路径
            source: 音源类型，AudioSource.MIC / SYSTEM / MIXED（默认 MIC）
            **source_kwargs: 传递给音源策略的参数（如 device_index）

        Returns:
            recording_start_time（time.time()），用于照片时间戳对齐

        Raises:
            RuntimeError: 已在录音中
            ImportError: 未安装所需音频库
        """
        with self._lock:
            if self._is_recording:
                raise RuntimeError("Already recording")

            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            self._wav_file = wave.open(output_path, "wb")
            self._wav_file.setnchannels(self.CHANNELS)
            self._wav_file.setsampwidth(self.SAMPLE_WIDTH)
            self._wav_file.setframerate(self.SAMPLE_RATE)

            self._output_path = output_path
            self._is_recording = True
            self._is_paused = False
            self._pause_start_time = None
            self._total_pause_duration = 0.0
            self._current_source = source
            self.recording_start_time = time.time()

            # 构造并开流音源策略（策略回调产出 16kHz mono int16 帧喂给 _on_frame）
            try:
                recorder_source = create_source(source, **source_kwargs)
            except Exception:
                # create_source 失败（如未知音源/缺依赖）时回滚已置位的状态，
                # 否则录音器永久卡在 Already recording 且 WAV 句柄泄漏。
                try:
                    self._wav_file.close()
                except Exception:
                    pass
                self._wav_file = None
                self._output_path = None
                self._is_recording = False
                self._current_source = AudioSource.MIC
                self.recording_start_time = None
                raise
            self._source = recorder_source
            started_at = self.recording_start_time

        # 开流可能立即触发回调，不能持有 recorder 锁，否则失败回滚时
        # source.close() 等待回调会产生锁互等。
        try:
            recorder_source.open(self._on_frame)
        except Exception:
            try:
                recorder_source.close()
            except Exception:
                pass
            with self._lock:
                if self._source is recorder_source:
                    self._is_recording = False
                    self._current_source = AudioSource.MIC
                    self._source = None
                    # 与内层 create_source 回滚对称：起点/输出路径一并复位，
                    # 否则失败后 elapsed_seconds()/photo_timestamp() 基于陈旧起点
                    self.recording_start_time = None
                    self._output_path = None
                    if self._wav_file is not None:
                        try:
                            self._wav_file.close()
                        except Exception:
                            pass
                        self._wav_file = None
            raise

        return started_at

    def start_recording_segmented(self, output_dir: str,
                                  recording_id: Optional[int] = None,
                                  source: str = AudioSource.MIC,
                                  **source_kwargs) -> float:
        """开始**分段录音模式**:按段写独立 wav 文件,配合 VAD 自动截断。

        与 ``start_recording`` 互斥(已在录音中会抛 RuntimeError)。启动后:
        - 立即开第 0 段 wav ``{output_dir}/rec_{rid}_seg0.wav``
        - ``rotate_segment()`` 关闭当前段并开下一段
        - ``stop_recording()`` 关闭最后一段并复位

        Args:
            output_dir: 段 wav 输出目录(自动创建)
            recording_id: 录音 id,用于文件名命名(可选,无则用时间戳)
            source: 音源类型,AudioSource.MIC / SYSTEM / MIXED(默认 MIC)
            **source_kwargs: 传递给音源策略的参数(如 device_index)

        Returns:
            recording_start_time(time.time())
        """
        with self._lock:
            if self._is_recording:
                raise RuntimeError("Already recording")

            Path(output_dir).mkdir(parents=True, exist_ok=True)
            self._segmented_mode = True
            self._segment_index = 0
            self._segment_output_dir = output_dir
            self._segment_recording_id = recording_id
            self._last_segment_path = None
            self._is_recording = True
            self._is_paused = False
            self._pause_start_time = None
            self._total_pause_duration = 0.0
            self._current_source = source
            self.recording_start_time = time.time()
            self._output_path = None  # 分段模式无单一 output_path
            self._segment_samples = 0

            try:
                # 开第 0 段 WAV（wave.open 可因磁盘满/无权限失败）
                self._open_segment_wav_locked(0)

                # 构造并开流音源策略
                recorder_source = create_source(source, **source_kwargs)
            except Exception:
                # 任一步骤失败都必须完整回滚状态并关闭已开句柄，
                # 否则录音器永久停留在"录音中"，后续 start_* 一律报 Already recording。
                if self._wav_file is not None:
                    try:
                        self._wav_file.close()
                    except Exception:
                        pass
                    self._wav_file = None
                self._segmented_mode = False
                self._is_recording = False
                self._current_source = AudioSource.MIC
                self._output_path = None
                self.recording_start_time = None
                raise
            self._source = recorder_source
            started_at = self.recording_start_time

        try:
            recorder_source.open(self._on_frame)
        except Exception:
            try:
                recorder_source.close()
            except Exception:
                pass
            with self._lock:
                if self._source is recorder_source:
                    self._is_recording = False
                    self._segmented_mode = False
                    self._current_source = AudioSource.MIC
                    self._source = None
                    # 与内层回滚对称：复位起点，避免陈旧时间戳泄漏到后续查询
                    self.recording_start_time = None
                    if self._wav_file is not None:
                        try:
                            self._wav_file.close()
                        except Exception:
                            pass
                        self._wav_file = None
            raise

        return started_at

    def _open_segment_wav_locked(self, idx: int) -> str:
        """打开指定段的 WAV 文件(调用方持 ``self._lock``)。

        关闭旧 wav(若有),开新 wav。返回新段路径。
        """
        if self._wav_file is not None:
            try:
                self._wav_file.close()
            except Exception:
                pass
            self._wav_file = None

        rid_part = (str(self._segment_recording_id)
                    if self._segment_recording_id is not None
                    else f"t{int(time.time())}")
        seg_path = str(Path(self._segment_output_dir) / f"rec_{rid_part}_seg{idx}.wav")
        wf = wave.open(seg_path, "wb")
        wf.setnchannels(self.CHANNELS)
        wf.setsampwidth(self.SAMPLE_WIDTH)
        wf.setframerate(self.SAMPLE_RATE)
        self._wav_file = wf
        return seg_path

    def rotate_segment(self) -> Tuple[str, float]:
        """截断当前段并开下一段(在分段模式下使用)。

        关闭当前 wav 文件(标记为 last_segment_path),开下一段新 wav。
        供 VAD 触发段截断时调用(由 GUI 主线程通过信号触发)。

        Returns:
            (closed_segment_path, closed_segment_duration_seconds)
            closed_segment_path 为刚关闭段的本地路径;duration 为该段时长。

        Raises:
            RuntimeError: 不在分段录音中
        """
        with self._lock:
            if not self._is_recording or not self._segmented_mode:
                raise RuntimeError("Not in segmented recording")

            # 计算当前段时长（按本段已写入的样本数精确统计）
            seg_path = self._segment_output_path_locked()
            duration = self._segment_duration_approx_locked()

            closed_idx = self._segment_index
            # 先把新段 WAV 完整打开成功后再切换；若打开失败保持当前段继续
            # 录音（索引不递增、旧 wav 不关闭），避免后续帧被静默丢弃。
            next_idx = self._segment_index + 1
            rid_part = (str(self._segment_recording_id)
                        if self._segment_recording_id is not None
                        else f"t{int(time.time())}")
            new_path = str(Path(self._segment_output_dir)
                           / f"rec_{rid_part}_seg{next_idx}.wav")
            new_wf = wave.open(new_path, "wb")
            new_wf.setnchannels(self.CHANNELS)
            new_wf.setsampwidth(self.SAMPLE_WIDTH)
            new_wf.setframerate(self.SAMPLE_RATE)

            old_wf = self._wav_file
            self._wav_file = new_wf
            self._segment_samples = 0
            if old_wf is not None:
                try:
                    old_wf.close()
                except Exception as e:
                    # close 失败意味着该段 WAV 头部落盘可能不完整，
                    # 留痕供排查（段仍按正常路径返回，调用方转写失败可对照）
                    logger.warning(f"rotate_segment: 关闭旧段 {seg_path} 失败: {e}")
            self._segment_index = next_idx
            self._last_segment_path = seg_path
            logger.info(
                f"rotate_segment: 段 {closed_idx} 关闭({duration:.1f}s) → "
                f"段 {self._segment_index} 开启 {new_path}"
            )
            # 返回关闭段的路径与时长(供调用方入库)
            return self._last_segment_path or seg_path, duration

    def _segment_output_path_locked(self) -> str:
        """当前段 wav 路径(持锁)。"""
        rid_part = (str(self._segment_recording_id)
                    if self._segment_recording_id is not None
                    else f"t{int(time.time())}")
        return str(Path(self._segment_output_dir)
                   / f"rec_{rid_part}_seg{self._segment_index}.wav")

    def _segment_duration_approx_locked(self) -> float:
        """当前段时长(持锁)，按本段已写入样本数统计。"""
        # DB 真实段起止仍以 VAD 时间戳为准，此处供日志与 rotate 返回值使用。
        return self._segment_samples / float(self.SAMPLE_RATE)

    @property
    def last_segment_path(self) -> Optional[str]:
        """最近一段(已关闭)的本地路径,供收尾转写使用。"""
        with self._lock:
            return self._last_segment_path

    @property
    def is_segmented_mode(self) -> bool:
        """当前是否处于分段录音模式。"""
        with self._lock:
            return self._segmented_mode

    @property
    def current_segment_index(self) -> int:
        """当前段序号(0-based)。"""
        with self._lock:
            return self._segment_index

    def _on_frame(self, indata: np.ndarray, frames: int) -> None:
        """音源策略回调：将 16kHz mono int16 帧写入 WAV 并计算实时音量。

        所有音源策略（mic/system/mixed）统一调用此方法，签名 ``on_frame(indata, frames)``。
        ``indata`` 为 int16、shape ``(frames, 1)``、16kHz mono。
        """
        # stop/rotate 会关闭或切换 wav，必须与写入串行化。尤其是 stop 先
        # 置为 False 再关底层流，晚到的回调会在这里安全丢弃。
        with self._lock:
            if not self._is_recording or self._wav_file is None:
                return
            if self._is_paused:
                # 暂停时丢弃帧，不计入 WAV 和音量
                self._current_level = 0.0
                return
            self._wav_file.writeframes(indata.tobytes())
            self._segment_samples += int(indata.shape[0])
            # 计算 RMS 音量，归一化到 0~1 供声波可视化。
            try:
                samples = indata[:, 0] if indata.ndim > 1 else indata.ravel()
                rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
                # 32768 为 int16 满量程；×4 增益让正常说话音量明显跳动
                self._current_level = min(1.0, (rms / 32768.0) * 4.0)
            except Exception:
                pass
        # 分发实时帧给订阅者（如实时转写 worker / VAD）。快照后逐个通知，
        # 单个订阅者异常不得影响录音主路径。
        with self._sub_lock:
            subs = tuple(self._subscribers)
        for cb in subs:
            try:
                cb(indata, frames)
            except Exception:
                pass

    def stop_recording(self) -> Optional[float]:
        """停止录音并关闭文件。

        分段模式下:关闭最后一段 WAV(不旋转),把最后段路径存入
        ``last_segment_path`` 供收尾转写读取,并复位分段状态。

        Returns:
            录音总时长（秒），未在录音时返回 None
        """
        # 不能在持有 recorder 锁时停止底层流：底层 stop 会等待音频回调，
        # 而回调需要同一把锁写 WAV，二者会形成死锁。
        with self._lock:
            if not self._is_recording:
                return None

            self._is_recording = False
            self._is_paused = False
            self._pause_start_time = None
            self._current_level = 0.0
            source = self._source
            self._source = None
            self._stream = None  # 兼容旧字段

        # 在锁外等待底层回调退出；此时 _on_frame 会因 _is_recording=False 直接返回。
        if source is not None:
            try:
                source.close()
            except Exception:
                logger.exception("关闭音频流失败")

        with self._lock:

            # 分段模式:关闭最后一段 wav(若有内容)并记录路径
            if self._segmented_mode:
                if self._wav_file is not None:
                    # 记录最后段路径(若文件已开但未写入帧,仍视为有效段,
                    # 由调用方按 VAD 时间戳决定是否转写)
                    try:
                        last_path = self._segment_output_path_locked()
                    except Exception:
                        last_path = None
                    try:
                        self._wav_file.close()
                    except Exception:
                        pass
                    self._last_segment_path = last_path
                self._wav_file = None
            else:
                # 旧模式:关闭单 wav 文件
                if self._wav_file is not None:
                    try:
                        self._wav_file.close()
                    except Exception:
                        pass
                    self._wav_file = None

            duration = None
            if self.recording_start_time is not None:
                duration = time.time() - self.recording_start_time
            # 复位起点，保证 stop 后 elapsed_seconds()=0、photo_timestamp()
            # 按契约抛 RuntimeError，而不是基于陈旧起点返回无意义值。
            self.recording_start_time = None
            # 复位分段状态(保留 last_segment_path 供外部读取,
            # 下次 start_recording_segmented 会重置)
            if self._segmented_mode:
                logger.info(
                    f"stop_recording(分段, source={self._current_source}): "
                    f"共 {self._segment_index + 1} 段, "
                    f"总时长 {duration:.1f}s, last_segment={self._last_segment_path}"
                )
                self._segmented_mode = False
                # _segment_index / _last_segment_path 保留到下次 start_*_segmented 调用
                # (中间供 stop 后的收尾流程读取)
            else:
                logger.info(
                    f"stop_recording(source={self._current_source}): "
                    f"总时长 {duration:.1f}s"
                )
            self._current_source = AudioSource.MIC
            return duration

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def is_paused(self) -> bool:
        """当前是否处于暂停状态。"""
        return self._is_paused

    def pause(self) -> None:
        """暂停录音（不停止音频流，仅丢弃帧）。

        暂停期间 WAV 不写入新数据，音量归零，计时器扣除暂停时长。
        可多次调用，仅首次生效。
        """
        with self._lock:
            if not self._is_recording or self._is_paused:
                return
            self._is_paused = True
            self._pause_start_time = time.time()
            self._current_level = 0.0
        logger.info("录音已暂停")

    def resume(self) -> None:
        """恢复录音。

        可多次调用，仅在暂停状态时生效。
        """
        with self._lock:
            if not self._is_paused:
                return
            if self._pause_start_time is not None:
                self._total_pause_duration += time.time() - self._pause_start_time
                self._pause_start_time = None
            self._is_paused = False
        logger.info("录音已恢复")

    @property
    def output_path(self) -> Optional[str]:
        return self._output_path

    @property
    def current_source(self) -> str:
        """当前录音音源类型（AudioSource.MIC/SYSTEM/MIXED）。"""
        return self._current_source

    def elapsed_seconds(self) -> float:
        """当前录音已过秒数（未录音返回 0，暂停时扣除暂停时长）。"""
        if self.recording_start_time is None:
            return 0.0
        elapsed = time.time() - self.recording_start_time
        # 扣除已累计的暂停时长
        elapsed -= self._total_pause_duration
        # 若当前正在暂停，扣除本次暂停已过的时间
        if self._is_paused and self._pause_start_time is not None:
            elapsed -= time.time() - self._pause_start_time
        return max(0.0, elapsed)

    def current_level(self) -> float:
        """当前麦克风音量（0.0~1.0），供声波可视化驱动。未录音返回 0。"""
        return self._current_level

    def photo_timestamp(self) -> float:
        """拍照时刻相对录音内容的秒数（用于 RecordingAttachment.timestamp）。

        扣除暂停时长：暂停期间帧不写入 WAV，音频时间线冻结，
        时间戳必须与音频内容对齐而非墙钟。
        """
        if self.recording_start_time is None:
            raise RuntimeError("Not recording; cannot compute photo timestamp")
        ts = time.time() - self.recording_start_time - self._total_pause_duration
        if self._is_paused and self._pause_start_time is not None:
            ts -= time.time() - self._pause_start_time
        return max(0.0, ts)

    @staticmethod
    def list_input_devices() -> list:
        """列出可用麦克风输入设备（供 GUI 选择）。"""
        from core.audio.sources import _MicSource
        return _MicSource.list_devices()

    @staticmethod
    def list_audio_sources() -> dict:
        """列出所有可用音源及设备（麦克风 + 系统音频 loopback）。

        返回结构：
        ::
            {
                "mic": [{"index": 0, "name": "...", "channels": 1, "type": "mic"}],
                "system": [{"index": 5, "name": "...", "channels": 2, "rate": 48000, "type": "system"}],
                "mic_available": True,
                "system_available": True,
            }
        """
        from core.audio.sources import list_all_audio_sources
        return list_all_audio_sources()
