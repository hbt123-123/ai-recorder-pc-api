"""core/audio 纯内存逻辑单元测试（stdlib unittest，无需 pytest）。

覆盖回归风险最高的两块确定性逻辑：
- VADSegmenter 三态状态机（WAITING/SPEAKING/SILENCE）的截断边界
- _MixedSource._try_drain 双路配对 + 降级粘滞逻辑

运行：python tests/test_audio_logic.py
"""

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audio.sources import (  # noqa: E402
    _MixedSource,
    _MIXED_DEGRADE_THRESHOLD,
    _MIXED_MAX_BUF_FRAMES,
)
from core.audio.vad_segmenter import VADSegmenter  # noqa: E402

SR = 16000


def speech_chunk(frames: int) -> np.ndarray:
    """非静音 int16 帧（RMS 远超阈值）。"""
    rng = np.random.default_rng(42)
    return (rng.integers(-20000, 20000, size=(frames, 1))).astype(np.int16)


def silence_chunk(frames: int) -> np.ndarray:
    return np.zeros((frames, 1), dtype=np.int16)


class VADSegmenterTest(unittest.TestCase):
    """VAD 三态状态机：喂确定性帧序列驱动。"""

    def _make(self, **overrides) -> tuple:
        finalized = []

        def on_finalize(seg_idx, start_s, end_s):
            finalized.append((seg_idx, start_s, end_s))

        params = dict(
            silence_threshold=0.02,
            silence_duration=1.5,
            min_segment_seconds=1.0,
            max_segment_seconds=270.0,
            on_segment_finalize=on_finalize,
            sample_rate=SR,
        )
        params.update(overrides)
        vad = VADSegmenter(**params)
        vad.start()
        return vad, finalized

    @staticmethod
    def _feed(vad: VADSegmenter, chunk_fn, seconds: float,
              step: float = 0.25) -> None:
        """按小步长喂帧：状态机的截断判定发生在后续帧到达时，
        单个大块不会触发（与真实音频回调的连续小帧一致）。"""
        fed = 0.0
        while fed < seconds - 1e-9:
            dur = min(step, seconds - fed)
            frames = int(dur * SR)
            vad.consume_frame(chunk_fn(frames), frames)
            fed += dur

    def test_noise_burst_below_min_segment_discarded(self):
        # 短噪音(0.3s) + 快速静音截断(0.5s)：段总时长 0.8s < min(1.0s) 应丢弃
        vad, finalized = self._make(silence_duration=0.5)
        self._feed(vad, speech_chunk, 0.3, step=0.1)
        self._feed(vad, silence_chunk, 0.7, step=0.1)
        self.assertEqual(finalized, [])
        self.assertIsNone(vad.stop())

    def test_silence_triggers_segment_finalize(self):
        vad, finalized = self._make()
        self._feed(vad, speech_chunk, 2.0)
        self._feed(vad, silence_chunk, 3.0)
        self.assertEqual(len(finalized), 1)
        seg_idx, start, end = finalized[0]
        self.assertEqual(seg_idx, 0)
        self.assertAlmostEqual(start, 0.0)
        self.assertAlmostEqual(end, 3.5)

    def test_brief_pause_does_not_split_segment(self):
        vad, finalized = self._make()
        # 语音 1.5s + 静音 0.5s(< silence_duration) + 语音 1.5s → 单段不截断
        self._feed(vad, speech_chunk, 1.5)
        self._feed(vad, silence_chunk, 0.5)
        self._feed(vad, speech_chunk, 1.5)
        self.assertEqual(finalized, [])
        # 停止后返回完整段 [0, 3.5]
        last = vad.stop()
        self.assertIsNotNone(last)
        _, start, end = last
        self.assertAlmostEqual(start, 0.0)
        self.assertAlmostEqual(end, 3.5)

    def test_max_segment_force_cut(self):
        vad, finalized = self._make(max_segment_seconds=5.0)
        # 持续语音 6s，超过 max_segment(5s) 应强制截断一次，余量留作末段
        self._feed(vad, speech_chunk, 6.0)
        self.assertEqual(len(finalized), 1)
        seg_idx, start, end = finalized[0]
        self.assertEqual(seg_idx, 0)
        self.assertAlmostEqual(end - start, 5.0)

    def test_stop_returns_active_tail_segment(self):
        vad, finalized = self._make()
        vad.consume_frame(speech_chunk(2 * SR), 2 * SR)
        last = vad.stop()
        self.assertIsNotNone(last)
        self.assertEqual(finalized, [])  # stop 返回的末段不走回调


class MixedSourceDrainTest(unittest.TestCase):
    """_MixedSource._try_drain 配对/降级逻辑（不开真实音频流）。"""

    def _make_source(self) -> _MixedSource:
        src = _MixedSource.__new__(_MixedSource)  # 跳过 __init__ 避免开设备
        from collections import deque
        from core.audio.sources import _MicSource, _SystemLoopbackSource
        src._mic = MagicMock(spec=_MicSource)
        src._system = MagicMock(spec=_SystemLoopbackSource)
        src._on_frame = None
        src._mic_buf = deque()
        src._system_buf = deque()
        src._mic_lock = threading.Lock()
        src._system_lock = threading.Lock()
        src._lag_warned = {"mic": False, "system": False}
        src._degraded = {"mic": False, "system": False}
        return src

    def _collect_emit(self, src: _MixedSource) -> list:
        emitted = []
        src._on_frame = lambda arr, frames: emitted.append((arr.copy(), frames))
        return emitted

    def test_pairing_mixes_both_channels(self):
        src = self._make_source()
        emitted = self._collect_emit(src)
        mic = speech_chunk(1000)
        sys_audio = speech_chunk(800)
        with src._mic_lock:
            src._mic_buf.append(mic)
        with src._system_lock:
            src._system_buf.append(sys_audio)
        src._try_drain()
        self.assertEqual(len(emitted), 1)
        arr, frames = emitted[0]
        self.assertEqual(frames, 800)  # 取两路最小长度
        self.assertEqual(len(src._mic_buf), 1)   # mic 余量回队等待配对
        self.assertEqual(len(src._system_buf), 0)

    def test_single_side_below_threshold_waits(self):
        src = self._make_source()
        emitted = self._collect_emit(src)
        n = _MIXED_DEGRADE_THRESHOLD // 4
        with src._mic_lock:
            src._mic_buf.append(speech_chunk(n))
        src._try_drain()
        self.assertEqual(emitted, [])           # 未达阈值不放行
        self.assertFalse(src._degraded["mic"])  # 未进入降级

    def test_degrade_after_threshold_then_sticky(self):
        src = self._make_source()
        emitted = self._collect_emit(src)
        # 第一批达阈值 → 进入降级并直出
        n = _MIXED_DEGRADE_THRESHOLD
        with src._mic_lock:
            src._mic_buf.append(speech_chunk(n))
        src._try_drain()
        self.assertTrue(src._degraded["mic"])
        self.assertEqual(len(emitted), 1)
        # 第二批单路小数据：粘滞降级下立即直出（无需再等阈值）
        with src._mic_lock:
            src._mic_buf.append(speech_chunk(100))
        src._try_drain()
        self.assertEqual(len(emitted), 2)

    def test_overflow_drops_oldest_and_bounds_memory(self):
        src = self._make_source()
        self._collect_emit(src)  # 挂上 emit 收集器（溢出路径不 emit，应为空）
        # 经 _append_chunk 灌入超上限巨块：只保留最新尾部，总量有界
        big = speech_chunk(_MIXED_MAX_BUF_FRAMES * 3)
        src._append_chunk(src._mic_buf, src._mic_lock, big, "mic")
        with src._mic_lock:
            total = sum(len(c) for c in src._mic_buf)
            self.assertLessEqual(total, _MIXED_MAX_BUF_FRAMES)
            # 保留的是最新尾部（首块 == 原块的最后上限帧）
            self.assertEqual(len(src._mic_buf[0]), _MIXED_MAX_BUF_FRAMES)
            np.testing.assert_array_equal(
                src._mic_buf[0], big[-_MIXED_MAX_BUF_FRAMES:])

    def test_dual_snapshot_is_atomic_no_interleave_loss(self):
        """两路并发 append 时快照复合原子：不出现整块被拆到两个周期的丢失。

        守恒校验：emit 总帧数 + 结束时缓冲内帧数 ≈ 投喂总帧数（64 帧小块
        配对粒度下，未 emit 的余量必然仍留在 deque 中）。
        """
        src = self._make_source()
        emitted_frames = []
        stop = threading.Event()

        def collector(arr, frames):
            emitted_frames.append(frames)

        src._on_frame = collector
        fed = {"mic": 0, "system": 0}

        def feed_mic():
            while not stop.is_set():
                src._append_chunk(src._mic_buf, src._mic_lock,
                                  speech_chunk(64), "mic")
                fed["mic"] += 64
                src._try_drain()

        def feed_system():
            while not stop.is_set():
                src._append_chunk(src._system_buf, src._system_lock,
                                  speech_chunk(64), "system")
                fed["system"] += 64
                src._try_drain()

        t1 = threading.Thread(target=feed_mic)
        t2 = threading.Thread(target=feed_system)
        t1.start(); t2.start()
        try:
            threading.Event().wait(0.3)
        finally:
            stop.set()
            t1.join(); t2.join()

        self.assertGreater(sum(emitted_frames), 0)  # 混音确有产出
        with src._mic_lock:
            buffered_mic = sum(len(c) for c in src._mic_buf)
        with src._system_lock:
            buffered_sys = sum(len(c) for c in src._system_buf)
        # 守恒：产出 + 缓冲 ≈ 投喂（允许最后一个 64 块在两线程停止间隙的
        # 少量不对称，误差不超过单块大小 ×2）
        total_out = sum(emitted_frames) + buffered_mic + buffered_sys
        total_in = fed["mic"] + fed["system"]
        self.assertLessEqual(abs(total_out - total_in), 128)


if __name__ == "__main__":
    unittest.main(verbosity=2)
