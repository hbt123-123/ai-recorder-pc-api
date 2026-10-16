"""VAD 静音检测分段器:在音频线程跟踪 RMS,触发段截断。

设计目标:
- 让批处理 tier(free/pro_lite/pro_pro)在不改连接器的前提下达到接近实时版
  的体验:说话停顿达阈值即截断当前段,触发独立转写,UI 增量显示。
- 复用 ``AudioRecorder.add_subscriber`` 订阅机制(与 realtime worker 同源),
  ``consume_frame`` 在 sounddevice 音频线程被调用,仅做 RMS 计算 + 状态机
  推进,不阻塞、不抛异常。

时间戳机制(关键):
- 基于**音频帧累计时间**(``samples_consumed / sample_rate``)而非墙钟 ``time.time()``。
- 原因:① 测试可快速喂帧而不必等待真实墙钟;② 真实录音时音频时间是媒体
  时间,不受 Python 调度抖动影响,段时长与转写 segments 时间戳同源对齐。
- ``start(recording_start_time)`` 仍接收 ``recording_start_time`` 参数仅用于
  日志兼容,状态机不依赖它。

状态机(WAITING / SPEAKING / SILENCE):
- WAITING:段未开始(段起始或截断后)。首帧非静音 → SPEAKING 并打开段。
- SPEAKING:段内持续有语音。
  - 出现静音 → SILENCE(开始累计静音时长)。
  - 段累积时长 ≥ max_segment_seconds → 强制截断(规避 pro_pro 5min 上限)。
- SILENCE:段内连续静音。
  - 静音时长 ≥ silence_duration → 截断当前段,切回 WAITING。
  - 中途出现语音 → 切回 SPEAKING,清空静音计时。
  - 段累积时长 ≥ max_segment_seconds → 强制截断。

线程安全:
- ``_update_state`` 内部持锁更新状态,**返回待回调事件**(seg_idx, start, end)
  或 None;锁外由 ``consume_frame`` 调用回调,避免回调内争用导致死锁。
- 回调 ``on_segment_finalize(seg_idx, start_s, end_s)`` 必须非阻塞
  (典型实现:Qt signal emit 或 Queue.put)。
"""

import logging
import threading
from typing import Callable, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class VADSegmenter:
    """VAD 静音分段器:订阅音频帧,管理段切换状态机。"""

    def __init__(
        self,
        silence_threshold: float = 0.02,      # 归一化 RMS(0~1),约 -34dB
        silence_duration: float = 1.5,          # 触发截断的连续静音秒数
        min_segment_seconds: float = 1.0,       # 短于此则丢弃(噪音过滤)
        max_segment_seconds: float = 270.0,     # 强制截断上限(pro_pro 5min 留 30s 余量)
        on_segment_finalize: Optional[Callable[[int, float, float], None]] = None,
        sample_rate: int = 16000,                # 音频采样率,用于把帧数换算为秒
    ):
        self.silence_threshold = float(silence_threshold)
        self.silence_duration = float(silence_duration)
        self.min_segment_seconds = float(min_segment_seconds)
        self.max_segment_seconds = float(max_segment_seconds)
        self.sample_rate = int(sample_rate)
        self._on_finalize = on_segment_finalize

        self._lock = threading.Lock()
        self._state = "WAITING"  # WAITING / SPEAKING / SILENCE
        self._segment_start: Optional[float] = None    # 当前段起始(音频相对秒)
        self._silence_start: Optional[float] = None    # 当前静音起始(音频相对秒)
        self._segment_index = 0
        # 已消费的音频采样数(音频时间 = _samples_consumed / sample_rate)。
        # 状态机时间戳全部基于此,不依赖墙钟。
        self._samples_consumed = 0
        self._recording_start: Optional[float] = None  # 仅日志用,状态机不用
        self._stopped = True

    # ---------- 生命周期 ----------

    def start(self, recording_start_time: Optional[float] = None) -> None:
        """开始追踪。

        Args:
            recording_start_time: 主录音起始 ``time.time()``(AudioRecorder
                ``recording_start_time`` 属性),仅用于日志,状态机不依赖。
                状态机时间戳基于音频帧累计(``samples_consumed / sample_rate``),
                与墙钟解耦,确保测试可快速喂帧、生产环境免受调度抖动影响。
        """
        with self._lock:
            self._state = "WAITING"
            self._segment_start = None
            self._silence_start = None
            self._segment_index = 0
            self._samples_consumed = 0
            self._recording_start = (float(recording_start_time)
                                     if recording_start_time is not None else None)
            self._stopped = False
            logger.info(
                f"VAD 启动: threshold={self.silence_threshold} "
                f"silence={self.silence_duration}s min={self.min_segment_seconds}s "
                f"max={self.max_segment_seconds}s sr={self.sample_rate}"
            )

    def stop(self) -> Optional[Tuple[int, float, float]]:
        """停止追踪。

        Returns:
            (segment_index, start_s, end_s) 最后未截断段信息,供调用方收尾转写;
            若当前无活动段(段起始为 None 或段过短且丢弃)返回 None。
            注意:此处返回的段不会经过 ``on_segment_finalize`` 回调,调用方需
            自行决定是否触发该段转写。
        """
        with self._lock:
            self._stopped = True
            if self._segment_start is None:
                return None
            # 用音频时间(非墙钟)计算末段结束时刻,与段截断时间戳同源
            end_time = self._samples_consumed / self.sample_rate
            duration = end_time - self._segment_start
            if duration < self.min_segment_seconds:
                logger.debug(
                    f"VAD stop: 末段 {self._segment_index} 过短({duration:.1f}s),丢弃"
                )
                return None
            return (self._segment_index, self._segment_start, end_time)

    # ---------- 音频线程入口 ----------

    def consume_frame(self, indata, frames: int) -> None:
        """音频线程回调:推进状态机。

        ``indata`` 为 int16、shape (frames, 1) 或 (frames,) 的音频块。计算
        归一化 RMS(0~1,不乘可视化增益),与 ``silence_threshold`` 比较决定状态
        迁移。触发段截断时**在锁外**调用回调。

        时间戳基于音频帧累计(``samples_consumed / sample_rate``),与墙钟解耦。
        """
        if self._stopped:
            return
        try:
            samples = indata[:, 0] if indata.ndim > 1 else indata.ravel()
            rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
            normalized = rms / 32768.0  # 归一化到 0~1
            pending = self._update_state(normalized, int(frames))
            if pending is not None:
                cb = self._on_finalize
                if cb is not None:
                    cb(*pending)  # 锁外回调
        except Exception:
            # 音频线程内禁止阻塞式 stderr 输出（print_exc 在管道堵塞时会
            # 卡住回调导致丢帧），仅保留结构化日志。
            logger.debug("VAD 帧处理异常", exc_info=True)

    # ---------- 状态机(锁内) ----------

    def _update_state(
        self, level: float, frames: int
    ) -> Optional[Tuple[int, float, float]]:
        """推进状态机(**内部持锁**,调用方无需持锁)。

        .. warning::
            ``self._lock`` 为不可重入 ``threading.Lock``。调用方**不得**在已
            持有 ``self._lock`` 的状态下调用本方法,否则同线程二次 acquire 会
            死锁。正确的入口是 ``consume_frame``(音频线程,不持锁调用本方法)。

        Args:
            level: 归一化 RMS(0~1)。
            frames: 本帧采样数,用于累计音频时间。

        Returns:
            待回调事件 (seg_idx, start_s, end_s) 或 None。
        """
        with self._lock:
            # 当前帧的时间戳(帧开始时刻),基于音频采样数累计
            now = self._samples_consumed / self.sample_rate
            # 消费本帧采样(下一帧的时间戳将基于更新后的累计值)
            self._samples_consumed += frames

            is_speech = level >= self.silence_threshold
            pending: Optional[Tuple[int, float, float]] = None

            if self._state == "WAITING":
                if is_speech:
                    self._state = "SPEAKING"
                    self._segment_start = now
                    self._silence_start = None

            elif self._state == "SPEAKING":
                if not is_speech:
                    self._state = "SILENCE"
                    self._silence_start = now
                else:
                    # 段累积达上限,强制截断
                    if (now - self._segment_start) >= self.max_segment_seconds:
                        pending = self._finalize_segment_locked(now)

            elif self._state == "SILENCE":
                if is_speech:
                    # 静音打断,继续累积当前段
                    self._state = "SPEAKING"
                    self._silence_start = None
                else:
                    if (now - self._silence_start) >= self.silence_duration:
                        pending = self._finalize_segment_locked(now)
                    elif (now - self._segment_start) >= self.max_segment_seconds:
                        pending = self._finalize_segment_locked(now)

            return pending

    def _finalize_segment_locked(
        self, now: float
    ) -> Optional[Tuple[int, float, float]]:
        """截断当前段(锁内调用),返回待回调事件或 None(段过短丢弃)。

        状态重置为 WAITING,``segment_index`` 递增。
        """
        if self._segment_start is None:
            self._state = "WAITING"
            return None
        duration = now - self._segment_start
        seg_idx = self._segment_index
        seg_start = self._segment_start
        seg_end = now
        # 状态重置:进入下一段等待
        self._segment_index += 1
        self._segment_start = None
        self._silence_start = None
        self._state = "WAITING"
        if duration < self.min_segment_seconds:
            logger.debug(f"VAD: 段 {seg_idx} 过短({duration:.1f}s),丢弃")
            return None
        logger.info(
            f"VAD: 段 {seg_idx} 截断 [{seg_start:.1f}-{seg_end:.1f}s] dur={duration:.1f}s"
        )
        return (seg_idx, seg_start, seg_end)
