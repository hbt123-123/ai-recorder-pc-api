"""音源策略抽象：麦克风 / 系统音频 loopback / 混音。

每种策略各自负责开流 + 在回调内产出 **16kHz / 16-bit / mono** 的 int16 帧，
喂给 AudioRecorder 的统一处理逻辑（写 WAV / 算音量 / 通知订阅者）。

设计要点：
- 所有策略的回调签名一致：``on_frame(indata: np.ndarray, frames: int)``
  ``indata`` 为 int16、shape ``(frames, 1)``、16kHz mono 只读视图。
- 重采样优先 ``soxr``，import 失败回退 ``scipy.signal.resample_poly``。
- ``pyaudiowpatch`` / ``sounddevice`` 均延迟导入，避免无硬件/未装依赖时 import 失败。
- 回调在独立音频线程，必须非阻塞；重采样用预分配缓冲区，避免回调内动态分配。

WASAPI loopback 采集 Windows 系统音频（会议软件播放的对方语音、共享音视频），
覆盖会议场景下"对方说话在电脑系统音频里，麦克风收不到"的缺口。
"""

import logging
import threading
from collections import deque
from typing import Callable, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# 目标格式常量（与 AudioRecorder 对齐，全链路 ASR/VAD 依赖）
TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1

# 系统音频常见采样率（WASAPI 通常 48kHz）
SYSTEM_SAMPLE_RATE = 48000
SYSTEM_CHANNELS = 2

# 混音模式单路缓冲帧数上限(1 秒 @ 16kHz mono)。两路异步回调 drain 时机不同步,
# 一路持续到达另一路滞后时,缓冲可能堆积。超过上限丢弃最旧帧以保护内存,
# 极端情况下可能产生轻微音频断裂(正常 drain 周期远小于此)。
_MIXED_MAX_BUF_FRAMES = 16000

# 混音降级阈值(帧数)。某路长时间无声时(典型场景:WASAPI loopback 在系统静音时
# 不触发回调),另一路累积达此阈值后进入降级模式,单路直出避免音频被持续丢弃。
# 0.5 秒 @ 16kHz,与对齐滞后告警阈值一致:短暂间隙(自然停顿)不触发降级,
# 真正静音(0.5s 无数据)才降级。
_MIXED_DEGRADE_THRESHOLD = _MIXED_MAX_BUF_FRAMES // 2  # 8000


# ---------- 重采样工具 ----------

def _numpy_resample(x: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
    """numpy 线性插值重采样（soxr 不可用时的快速回退）。

    相比 scipy.signal.resample_poly 的 FFT 实现,np.interp 在音频回调中执行速度
    快得多(无 FFT 开销),适合实时回调路径。质量略低于 soxr 多相滤波器,但语音
    频段(16kHz)完全够用。仅支持 1D mono 输入(本场景 _stereo_to_mono 后已是 mono)。

    Args:
        x: float32 1D 数组(mono 样本)
        in_rate/out_rate: 采样率

    Returns:
        float32 1D 数组,长度按 out_rate/in_rate 比例缩放
    """
    if in_rate == out_rate:
        return x
    n_in = len(x)
    if n_in == 0:
        return x
    n_out = int(round(n_in * out_rate / in_rate))
    if n_out < 1:
        n_out = 1
    if n_in == 1:
        # 单样本边界:np.linspace(0,0,n_out) 全 0,np.interp 退化为常量,
        # 直接 repeat 避免返回错误
        return np.repeat(x, n_out).astype(np.float32, copy=False)
    # 线性插值索引:[0, n_in-1] 均匀采样 n_out 个点
    idx = np.linspace(0.0, n_in - 1, n_out)
    return np.interp(idx, np.arange(n_in), x).astype(np.float32, copy=False)


def _load_resampler():
    """加载重采样库，返回 callable(x, in_rate, out_rate) -> np.ndarray。

    优先 soxr（高质量快速,多相滤波器），失败回退 numpy 线性插值(快速,质量略低)。
    两者均不可用时抛 RuntimeError(numpy 为必装依赖,理论上不会失败)。

    .. note::
        历史回退用 scipy.signal.resample_poly,但其基于 FFT,在音频回调中执行
        过慢会导致卡顿丢帧,故改为 numpy 线性插值。
    """
    try:
        import soxr
        def soxr_resample(x: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
            # soxr 接受 float32/float64，返回 float64
            x_f = x.astype(np.float32, copy=False)
            y = soxr.resample(x_f, in_rate, out_rate, quality="HQ")
            return y
        return soxr_resample, "soxr"
    except ImportError:
        pass

    # numpy 线性插值回退(np 为必装依赖,不会 ImportError)
    return _numpy_resample, "numpy"


# 全局懒加载重采样器（首次使用时初始化，避免 import 时失败）
_resampler = None
_resampler_name = None


def _get_resampler():
    """懒加载全局重采样器。"""
    global _resampler, _resampler_name
    if _resampler is None:
        _resampler, _resampler_name = _load_resampler()
        logger.info(f"重采样器加载: {_resampler_name}")
    return _resampler


def _resample_to_16k(mono_samples: np.ndarray, in_rate: int) -> np.ndarray:
    """把 float32 mono 样本重采样到 16kHz，返回 float32。"""
    if in_rate == TARGET_SAMPLE_RATE:
        return mono_samples
    resampler = _get_resampler()
    return resampler(mono_samples, in_rate, TARGET_SAMPLE_RATE)


def _stereo_to_mono(stereo: np.ndarray) -> np.ndarray:
    """立体声 int16 -> mono float32（左右平均）。"""
    # stereo shape: (frames, 2)
    mono_f = stereo.astype(np.float32, copy=False).mean(axis=1)
    return mono_f


def _float_to_int16(x: np.ndarray) -> np.ndarray:
    """float32 -> int16，饱和裁剪到 [-32768, 32767]。"""
    return np.clip(np.round(x), -32768, 32767).astype(np.int16)


def _mix_int16(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """两路 int16 mono 等长帧相加，int32 累积后饱和裁剪回 int16。"""
    summed = a.astype(np.int32) + b.astype(np.int32)
    return np.clip(summed, -32768, 32767).astype(np.int16)


# ---------- 音源枚举 ----------

class AudioSource:
    """音源类型常量。"""
    MIC = "mic"          # 仅麦克风（现有行为）
    SYSTEM = "system"    # 仅系统音频 loopback
    MIXED = "mixed"      # 系统音频 + 麦克风混音


# ---------- 策略基类 ----------

class _AudioSourceStrategy:
    """音源策略基类：开流后通过回调产出 16kHz mono int16 帧。"""

    def open(self, on_frame: Callable[[np.ndarray, int], None]) -> None:
        """开流。``on_frame(indata, frames)`` 在音频线程调用，必须非阻塞。

        Args:
            on_frame: 帧回调，``indata`` 为 int16 shape ``(frames, 1)`` 16kHz mono。
        """
        raise NotImplementedError

    def close(self) -> None:
        """关流，释放底层资源。"""
        raise NotImplementedError

    @staticmethod
    def list_devices() -> List[dict]:
        """列出该策略可用设备（供 GUI 选择/诊断）。"""
        raise NotImplementedError


# ---------- 麦克风音源（sounddevice，现有行为） ----------

class _MicSource(_AudioSourceStrategy):
    """麦克风音源：sounddevice 直接 16kHz/mono/int16 输入。"""

    def __init__(self, device_index: Optional[int] = None):
        self._stream = None
        self._on_frame = None
        self._device_index = device_index  # None = 系统默认输入设备

    def open(self, on_frame) -> None:
        import sounddevice as sd
        self._on_frame = on_frame
        self._stream = sd.InputStream(
            samplerate=TARGET_SAMPLE_RATE,
            channels=TARGET_CHANNELS,
            dtype="int16",
            device=self._device_index,
            callback=self._sd_callback,
        )
        self._stream.start()

    def _sd_callback(self, indata, frames, time_info, status) -> None:
        # sounddevice 直接给 int16 (frames, 1) 16kHz mono，无需重采样
        if self._on_frame is not None:
            self._on_frame(indata, frames)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    @staticmethod
    def list_devices() -> List[dict]:
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            return [
                {"index": i, "name": d["name"], "channels": d["max_input_channels"],
                 "type": "mic"}
                for i, d in enumerate(devices)
                if d["max_input_channels"] > 0
            ]
        except Exception:
            return []

    @staticmethod
    def available() -> bool:
        """麦克风是否可用。"""
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            return any(d["max_input_channels"] > 0 for d in devices)
        except Exception:
            return False


# ---------- 系统音频 loopback（pyaudiowpatch） ----------

class _SystemLoopbackSource(_AudioSourceStrategy):
    """系统音频 loopback 音源：WASAPI 采集 Windows 系统声音。

    48kHz/stereo/int16 -> 下混 mono -> 重采样 16kHz -> int16。
    """

    def __init__(self, device_index: Optional[int] = None):
        self._pa = None
        self._stream = None
        self._on_frame = None
        self._device_index = device_index  # None = 默认 loopback
        # 不依赖 PyAudio Stream 的私有 ``_rate`` 属性。不同版本的
        # pyaudiowpatch/PortAudio 未必暴露该属性，缺失时会让回调持续失败而无声。
        self._sample_rate = SYSTEM_SAMPLE_RATE
        self._channels = SYSTEM_CHANNELS
        # 预分配重采样中间缓冲（按典型块大小 1024 帧 @48k 估算）
        self._mono_buf = None

    def open(self, on_frame) -> None:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as e:
            raise RuntimeError(
                "未安装 pyaudiowpatch，无法采集系统音频。"
                "请运行 pip install pyaudiowpatch"
            ) from e

        self._pa = pyaudio.PyAudio()
        device = self._select_device(self._pa)
        if device is None:
            self._pa.terminate()
            self._pa = None
            raise RuntimeError("无可用 WASAPI loopback 设备")

        actual_rate = int(device.get("defaultSampleRate", SYSTEM_SAMPLE_RATE))
        actual_channels = int(device.get("maxInputChannels", SYSTEM_CHANNELS))
        if actual_rate <= 0 or actual_channels <= 0:
            self._pa.terminate()
            self._pa = None
            raise RuntimeError(f"无效的 WASAPI loopback 参数: rate={actual_rate}, channels={actual_channels}")
        self._sample_rate = actual_rate
        self._channels = actual_channels
        logger.info(
            f"系统音频 loopback 开流: device={device['name']} "
            f"rate={actual_rate} channels={actual_channels}"
        )

        self._on_frame = on_frame
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=actual_channels,
            rate=actual_rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=1024,
            stream_callback=self._pa_callback,
        )
        self._stream.start_stream()

    def _select_device(self, pa):
        """选择 loopback 设备：指定 index 优先，否则默认 WASAPI loopback。"""
        if self._device_index is not None:
            try:
                info = pa.get_device_info_by_index(self._device_index)
                if info.get("isLoopbackDevice") or "loopback" in info.get("name", "").lower():
                    return info
            except Exception:
                pass

        # 默认 WASAPI loopback
        try:
            return pa.get_default_wasapi_loopback()
        except Exception:
            # 回退：遍历找 isLoopbackDevice
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if info.get("isLoopbackDevice"):
                    return info
        return None

    def _pa_callback(self, in_data, frame_count, time_info, status) -> tuple:
        """pyaudiowpatch 回调：bytes -> mono 16kHz int16 -> on_frame。

        必须非阻塞：重采样用 numpy 向量化操作，无动态分配。
        """
        try:
            if self._on_frame is None:
                return (None, __import__("pyaudiowpatch").paContinue)

            if status:
                logger.warning(f"系统音频回调状态异常: {status}")

            # bytes -> int16 ndarray
            stereo = np.frombuffer(in_data, dtype=np.int16)
            # 使用开流时记录的参数。异常/截断块不能按长度猜通道后 reshape，
            # 否则会在回调中持续抛错并造成整段会议音频丢失。
            channels = self._channels
            expected_samples = frame_count * channels
            if frame_count <= 0 or len(stereo) != expected_samples:
                logger.warning(
                    "系统音频数据块长度异常: "
                    f"expected={expected_samples}, actual={len(stereo)}, channels={channels}"
                )
                return (None, __import__("pyaudiowpatch").paContinue)
            if channels > 1:
                stereo = stereo.reshape(-1, channels)
                mono_f = _stereo_to_mono(stereo)
            else:
                mono_f = stereo.astype(np.float32, copy=False)

            # 重采样到 16kHz
            mono_16k_f = _resample_to_16k(mono_f, self._sample_rate)
            mono_16k = _float_to_int16(mono_16k_f).reshape(-1, 1)

            self._on_frame(mono_16k, len(mono_16k))
        except Exception as e:
            logger.warning(f"系统音频回调异常: {e}")

        import pyaudiowpatch as pyaudio
        return (None, pyaudio.paContinue)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
        self._sample_rate = SYSTEM_SAMPLE_RATE
        self._channels = SYSTEM_CHANNELS

    @staticmethod
    def list_devices() -> List[dict]:
        """列出 WASAPI loopback 设备。"""
        try:
            import pyaudiowpatch as pyaudio
            pa = pyaudio.PyAudio()
            try:
                result = []
                for i in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(i)
                    if info.get("isLoopbackDevice"):
                        result.append({
                            "index": i,
                            "name": info["name"],
                            "channels": info["maxInputChannels"],
                            "rate": int(info.get("defaultSampleRate", 48000)),
                            "type": "system",
                        })
                return result
            finally:
                pa.terminate()
        except Exception:
            return []

    @staticmethod
    def available() -> bool:
        """系统音频 loopback 是否可用。"""
        try:
            import pyaudiowpatch as pyaudio
            pa = pyaudio.PyAudio()
            try:
                try:
                    pa.get_default_wasapi_loopback()
                    return True
                except Exception:
                    for i in range(pa.get_device_count()):
                        if pa.get_device_info_by_index(i).get("isLoopbackDevice"):
                            return True
                    return False
            finally:
                pa.terminate()
        except Exception:
            return False


# ---------- 混音音源（系统 + 麦克风） ----------

class _MixedSource(_AudioSourceStrategy):
    """混音音源：同时开麦克风与系统音频，各自重采样到 16kHz mono 后逐帧相加。

    两路流回调在不同音频线程触发，用 threading.Lock + 双 deque 同步：
    每路回调把帧 append 到自己的 deque，然后尝试 drain：取两 deque 最小长度，
    相加（饱和裁剪）后 emit。剩余帧留在 deque 等下次配对。

    缓冲防护:每路 deque 设上限 ``_MIXED_MAX_BUF_FRAMES``(1 秒),溢出时丢弃最旧帧
    并告警(避免一路持续到达、另一路滞后导致内存爆炸)。极端情况下丢弃会产生轻微
    音频断裂,但正常 drain 周期远小于上限。

    降级机制(关键):WASAPI loopback 在系统无声时**不触发回调**,system_buf 长期为空。
    若按"等两路配对"逻辑,mic_buf 会持续堆积至溢出,用户麦克风音频被全部丢弃
    (录音全程无声)。故单路累积达 ``_MIXED_DEGRADE_THRESHOLD``(0.5 秒)后进入降级模式,
    直接单路直出(等价于与静音混音),避免另一路被饿死。

    降级 flag 粘滞(sticky):系统音频可能间歇性产生帧(WASAPI 偶发回调),若每次系统
    产 1 帧就退出降级,系统再次静音时需重新等 0.5s 阈值,导致反复进/退降级(flapping),
    每次损失 0.5s 麦克风音频 + 重复告警。故降级 flag 一旦置位就不因单次系统帧退出,
    仅在 close() 时重置。降级模式下两路都有数据时仍正常混音(Case 1),仅 Case 2/3
    (单路)行为不同:降级时立即直出,未降级时等 0.5s 阈值。

    线程安全：每个 deque 单独持锁，drain 时按相同长度取，避免一路 drain 另一路
    正在 append 导致的错位。日志/IO 操作在锁外执行,避免阻塞音频回调线程。
    """

    def __init__(self, mic_device_index: Optional[int] = None,
                 system_device_index: Optional[int] = None):
        self._mic = _MicSource(device_index=mic_device_index)
        self._system = _SystemLoopbackSource(device_index=system_device_index)
        self._on_frame = None
        self._mic_buf = deque()
        self._system_buf = deque()
        self._mic_lock = threading.Lock()
        self._system_lock = threading.Lock()
        self._lag_warned = {"mic": False, "system": False}
        # 降级模式 flag:某路长时间无声时,另一路单路直出。
        # mic=True 表示系统无声、麦克风单路直出;system=True 反之。
        # 粘滞(sticky):一旦置位不因单次系统帧退出,避免 flapping;仅 close() 重置。
        self._degraded = {"mic": False, "system": False}

    def open(self, on_frame) -> None:
        self._on_frame = on_frame
        # 两路独立开流，各自回调 _on_mic_frame / _on_system_frame
        try:
            self._mic.open(self._on_mic_frame)
            self._system.open(self._on_system_frame)
        except Exception:
            # 混音启动必须是原子操作：系统 loopback 不可用时，已经启动的
            # 麦克风流也要立即关掉，否则会泄漏后台回调并让下次录音失败。
            self.close()
            raise

    def _append_chunk(self, buf, lock, chunk, key: str) -> None:
        """通用 append + 溢出防护(按帧数计,非 chunk 数)。

        buf 是 chunk(ndarray)的 deque,每个 chunk 含若干帧。上限 _MIXED_MAX_BUF_FRAMES
        单位是帧,必须按 ``sum(len(c) for c in buf)`` 计算总帧数后比较,不能直接用
        ``len(buf)``(那是 chunk 数,量纲不匹配,会导致防护永不触发)。

        lock 内 append + 计算总帧数 + 超限则 popleft 丢弃最旧 chunk(整块丢) +
        设 flag;lock 外检查 flag 打日志并清 flag(避免 IO 阻塞音频回调线程)。
        单个 chunk 本身超上限的边界:保留该 chunk 最新 _MIXED_MAX_BUF_FRAMES 帧的尾部。
        """
        overflow_frames = 0
        with lock:
            buf.append(chunk)
            total = sum(len(c) for c in buf)
            if total > _MIXED_MAX_BUF_FRAMES:
                # 整块丢弃最旧 chunk 直到 total <= 上限(popleft O(1))
                while buf and total > _MIXED_MAX_BUF_FRAMES:
                    dropped = buf.popleft()
                    total -= len(dropped)
                    overflow_frames += len(dropped)
                # 边界:单个 chunk 本身就超上限(极端大块),保留其最新尾部
                if not buf and len(chunk) > _MIXED_MAX_BUF_FRAMES:
                    buf.append(chunk[-_MIXED_MAX_BUF_FRAMES:])
        if overflow_frames > 0:
            logger.warning(
                f"_MixedSource {key} 缓冲溢出,丢弃 {overflow_frames} 帧(> {_MIXED_MAX_BUF_FRAMES})"
            )

    def _on_mic_frame(self, indata: np.ndarray, frames: int) -> None:
        """麦克风回调：append 到 buf，尝试 drain。"""
        # 复制一份（sounddevice 的 indata 是只读视图，回调返回后可能被复用）
        chunk = indata.copy()
        self._append_chunk(self._mic_buf, self._mic_lock, chunk, "mic")
        self._try_drain()

    def _on_system_frame(self, indata: np.ndarray, frames: int) -> None:
        """系统音频回调：append 到 buf，尝试 drain。"""
        chunk = indata.copy()
        self._append_chunk(self._system_buf, self._system_lock, chunk, "system")
        self._try_drain()

    def _try_drain(self) -> None:
        """配对取两路最小长度,相加后 emit;单路长时间无声时降级单路直出。

        四种情况:
        1. 两路都有数据 → 正常配对混音(降级 flag 保持,不退出 — 避免 flapping)
        2. 仅 mic 有数据,已降级 或 累积 ≥ 阈值 → 进入/维持降级,直接 emit mic
        3. 仅 system 有数据,已降级 或 累积 ≥ 阈值 → 进入/维持降级,直接 emit system
        4. 仅一路有数据且累积 < 阈值 → 放回 deque 等下次配对(短暂间隙不立即降级)

        降级 flag 粘滞:一旦进入降级,后续单路到达立即直出(无需再等阈值),即使
        另一路偶发产生帧也不退出(避免反复进/退降级,每次退出后重新等阈值会丢失
        0.5s 音频)。仅 close() 重置 flag。
        """
        if self._on_frame is None:
            return

        # 取出两路当前累积的帧。快照+清空必须是复合原子操作：两路回调
        # 各自持单锁 append，若分两次加锁快照，中间窗口会让同批数据被拆到
        # 两个 drain 周期里分别按单路分支处理，产生整段时间错位。
        # append 路径只持有单把锁、不嵌套获取另一把，故此处按固定顺序
        # 同时持有两锁不会死锁。
        with self._mic_lock:
            with self._system_lock:
                mic_chunks = list(self._mic_buf)
                self._mic_buf.clear()
                mic_total = sum(len(c) for c in mic_chunks)
                sys_chunks = list(self._system_buf)
                self._system_buf.clear()
                sys_total = sum(len(c) for c in sys_chunks)

        # 滞后告警:某路累积超过上限一半(8000 帧=0.5 秒)提示对齐滞后。
        # 降级模式下不再告警(已通过降级机制处理,避免周期性噪声日志)。
        self._warn_lag_if_needed("mic", mic_total)
        self._warn_lag_if_needed("system", sys_total)

        # 情况 1: 两路都有数据 → 正常配对混音。
        # 注意:不退出降级模式(即使 degraded=True 仍走此分支混音)。降级 flag 粘滞,
        # 避免系统间歇性产帧导致反复进/退降级(每次退出后系统再静音需重新等 0.5s 阈值)。
        if mic_chunks and sys_chunks:
            mic_arr = np.concatenate(mic_chunks, axis=0)
            sys_arr = np.concatenate(sys_chunks, axis=0)
            min_len = min(len(mic_arr), len(sys_arr))
            mixed = _mix_int16(mic_arr[:min_len], sys_arr[:min_len])

            if len(mic_arr) > min_len:
                with self._mic_lock:
                    self._mic_buf.append(mic_arr[min_len:])
            if len(sys_arr) > min_len:
                with self._system_lock:
                    self._system_buf.append(sys_arr[min_len:])

            try:
                self._on_frame(mixed, min_len)
            except Exception as e:
                logger.warning(f"混音 emit 异常: {e}")
            return

        # 情况 2: 仅 mic 有数据
        if mic_chunks and not sys_chunks:
            if self._degraded["mic"] or mic_total >= _MIXED_DEGRADE_THRESHOLD:
                # 已降级 或 累积超阈值 → 进入/维持降级,直接 emit mic
                if not self._degraded["mic"]:
                    self._degraded["mic"] = True
                    logger.warning(
                        f"_MixedSource 进入降级模式:系统音频无声("
                        f"mic 累积 {mic_total} 帧 ≥ {_MIXED_DEGRADE_THRESHOLD}),"
                        f"麦克风单路直出"
                    )
                mic_arr = np.concatenate(mic_chunks, axis=0)
                try:
                    self._on_frame(mic_arr, len(mic_arr))
                except Exception as e:
                    logger.warning(f"混音 emit 异常(降级 mic): {e}")
                return
            # 累积未达阈值:放回 deque 等下次配对
            with self._mic_lock:
                self._mic_buf.extend(mic_chunks)
            return

        # 情况 3: 仅 system 有数据(对称处理)
        if sys_chunks and not mic_chunks:
            if self._degraded["system"] or sys_total >= _MIXED_DEGRADE_THRESHOLD:
                if not self._degraded["system"]:
                    self._degraded["system"] = True
                    logger.warning(
                        f"_MixedSource 进入降级模式:麦克风无声("
                        f"system 累积 {sys_total} 帧 ≥ {_MIXED_DEGRADE_THRESHOLD}),"
                        f"系统音频单路直出"
                    )
                sys_arr = np.concatenate(sys_chunks, axis=0)
                try:
                    self._on_frame(sys_arr, len(sys_arr))
                except Exception as e:
                    logger.warning(f"混音 emit 异常(降级 system): {e}")
                return
            with self._system_lock:
                self._system_buf.extend(sys_chunks)
            return

        # 情况 4: 两路都空(理论不会到这,drain 由某路 append 触发)

    def _warn_lag_if_needed(self, key: str, total_frames: int) -> None:
        """某路累积帧数超过上限一半时告警一次(去重,lock 外调用)。

        降级模式下不告警:此时缓冲策略已切换为单路直出,滞后告警无意义且会
        在每次 drain 时反复触发,造成日志噪声。
        """
        if self._degraded["mic"] or self._degraded["system"]:
            return
        if total_frames > _MIXED_MAX_BUF_FRAMES // 2:
            if not self._lag_warned[key]:
                self._lag_warned[key] = True
                logger.warning(
                    f"_MixedSource {key} 对齐滞后({total_frames} 帧> "
                    f"{_MIXED_MAX_BUF_FRAMES // 2}),可能产生音频延迟"
                )
        else:
            # 恢复正常后重置 flag,允许下次再次告警
            self._lag_warned[key] = False

    def close(self) -> None:
        try:
            self._mic.close()
        except Exception:
            pass
        try:
            self._system.close()
        except Exception:
            pass
        with self._mic_lock:
            self._mic_buf.clear()
        with self._system_lock:
            self._system_buf.clear()
        # 重置降级/告警 flag,确保重开流时干净状态
        self._degraded = {"mic": False, "system": False}
        self._lag_warned = {"mic": False, "system": False}

    @staticmethod
    def list_devices() -> List[dict]:
        return _MicSource.list_devices() + _SystemLoopbackSource.list_devices()

    @staticmethod
    def available() -> bool:
        return _MicSource.available() and _SystemLoopbackSource.available()


# ---------- 工厂函数 ----------

def create_source(source: str, **kwargs) -> _AudioSourceStrategy:
    """按音源类型创建策略实例。

    Args:
        source: AudioSource.MIC / SYSTEM / MIXED
        **kwargs: 传递给具体策略的参数（如 device_index）

    Returns:
        _AudioSourceStrategy 实例

    Raises:
        ValueError: 未知音源类型
    """
    if source == AudioSource.MIC:
        return _MicSource()
    elif source == AudioSource.SYSTEM:
        return _SystemLoopbackSource(
            device_index=kwargs.get("system_device_index")
        )
    elif source == AudioSource.MIXED:
        return _MixedSource(
            mic_device_index=kwargs.get("mic_device_index"),
            system_device_index=kwargs.get("system_device_index"),
        )
    else:
        raise ValueError(f"未知音源类型: {source}")


def list_all_audio_sources() -> dict:
    """列出所有可用音源及设备（供 GUI 诊断展示）。"""
    return {
        "mic": _MicSource.list_devices(),
        "system": _SystemLoopbackSource.list_devices(),
        "mic_available": _MicSource.available(),
        "system_available": _SystemLoopbackSource.available(),
    }
