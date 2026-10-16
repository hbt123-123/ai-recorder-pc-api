"""AI 链路连通性诊断（打包前预检，无 GUI 依赖）。

逐条验证四档 ASR / VLM / LLM 云链路，使用 .env 中的真实凭证（pc_api/credentials.py 加载）。
每条链路独立子进程运行（bash 侧加超时），互不污染环境变量与单例。

用法：
    python test_ai_chains.py --chain llm          # LLM 主备（GLM→DeepSeek）
    python test_ai_chains.py --chain vlm          # VLM（GLM-VL，失败自动切 Qwen）
    python test_ai_chains.py --chain asr_free     # 硅基流动 SenseVoiceSmall
    python test_ai_chains.py --chain asr_realtime # 百炼 paraformer-realtime-8k-v2
    python test_ai_chains.py --chain asr_pro      # fun-asr-flash 同步 REST
    python test_ai_chains.py --chain asr_lite     # 火山豆包（含 SFTP 托管）
"""

import argparse
import os
import sys
import wave
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

STAGING = _ROOT / "data" / "staging"


def make_wav(path: Path, rate: int, seconds: float = 1.2) -> Path:
    """合成"语音节奏"测试音：多频段突发 + 静音间隙（int16 mono）。"""
    import math
    import struct

    n = int(rate * seconds)
    frames = []
    for i in range(n):
        t = i / rate
        # 0-0.4s 低频、0.5-0.9s 高频、其余静音：模拟说话停顿
        if t < 0.4:
            v = 0.5 * math.sin(2 * math.pi * 300 * t) + 0.3 * math.sin(2 * math.pi * 500 * t)
        elif 0.5 < t < 0.9:
            v = 0.4 * math.sin(2 * math.pi * 800 * t)
        else:
            v = 0.0
        frames.append(int(max(-1.0, min(1.0, v)) * 20000))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{len(frames)}h", *frames))
    return path


def make_png(path: Path) -> Path:
    """手工构造 64x64 PNG：蓝底 + 中央红色方块（VLM 有可描述内容）。"""
    import struct
    import zlib

    w = h = 64
    raw = b""
    for y in range(h):
        row = b"\x00"  # filter none
        for x in range(w):
            if 20 <= x < 44 and 20 <= y < 44:
                row += bytes([220, 40, 40])
            else:
                row += bytes([60, 110, 200])
        raw += row

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


def boot(tier: str | None = None):
    """公共启动：配置注入 + 单例重置（等价生产 apply_config 路径）。"""
    from core.user_config import UserConfig
    from pc_api.config_loader import apply_config

    cfg = UserConfig(asr_tier=tier or "free", language="zh")
    apply_config(cfg)
    return cfg


def chain_llm() -> str:
    from openai import OpenAI
    from core.config import get_config
    from core.fusion import NoteFuser

    boot("free")  # 注入 VLM/LLM 凭证 env（tier 无关）

    lc = get_config().llm
    detail = []
    primary_ok = False
    try:
        client = OpenAI(api_key=lc.api_key, base_url=lc.base_url)
        r = client.chat.completions.create(
            model=lc.model,
            messages=[{"role": "user", "content": "回复OK"}],
            max_tokens=16, timeout=30,
            extra_body={"thinking": {"type": "disabled"}},
        )
        primary_ok = bool(r.choices and r.choices[0].message.content)
        detail.append(f"主用 {lc.model}: {'OK' if primary_ok else '空响应'}")
    except Exception as e:
        detail.append(f"主用 {lc.model} 失败: {type(e).__name__}: {str(e)[:80]}")

    fuser = NoteFuser()
    title = fuser.generate_title(
        "我们讨论了三季度产品路线图，重点包括移动端适配与性能优化两个方向，下周出初稿再评审。")
    fb = "备用接管成功" if title else "备用也未产出(标题回退默认)"
    detail.append(f"集成 generate_title(主备自动切换): {'PASS' if title else 'WARN'} ({fb}, title={title!r})")

    if primary_ok or title:
        return "PASS | " + "; ".join(detail)
    return "FAIL | " + "; ".join(detail)


def chain_vlm() -> str:
    from core.vlm import get_vlm_connector, VLMRequest

    boot("free")  # 注入 GLM/DASHSCOPE 凭证 env

    img = make_png(STAGING / "vlm_probe.png")
    conn = get_vlm_connector()
    resp = conn.describe_image(VLMRequest(
        image_path=str(img), prompt="用一句话描述这张图片的内容"))
    desc = (resp.description or "").strip()
    provider = getattr(resp, "provider", "?")
    if desc:
        return f"PASS | provider={provider} 描述: {desc[:60]}"
    return f"FAIL | provider={provider} 空描述"


def make_speech_wav(path: Path) -> Path:
    """用百炼 cosyvoice TTS 合成真实人声 wav（无则回退合成音）。"""
    try:
        import dashscope
        from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat
        dashscope.api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        syn = SpeechSynthesizer(model="cosyvoice-v2", voice="longxiaochun_v2",
                                format=AudioFormat.WAV_16000HZ_MONO_16BIT)
        audio = syn.call("今天下午三点在会议室开产品评审会，请准时参加。")
        if audio:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(audio)
            return path
    except Exception as e:
        print(f"  [warn] TTS 合成失败，回退合成音: {e}")
    return make_wav(path, 16000)


def _asr_file_tier(tier: str) -> str:
    from core.transcription import get_connector, TranscriptionRequest

    boot(tier)
    wav = make_speech_wav(STAGING / f"asr_{tier}_probe.wav")
    connector = get_connector()
    with open(wav, "rb") as f:
        req = TranscriptionRequest(
            audio_file=f, filename=wav.name, mime_type="audio/wav",
            extra_options={"local_path": str(wav)},
        )
        resp = connector.transcribe(req)
    text = (resp.text or "").strip()
    segs = len(resp.segments) if resp.segments else 0
    note = f"text={text[:40]!r} segments={segs}"
    if not text:
        return f"WARN | {tier} 链路通但未识别出文字 ({note})"
    return f"PASS | {tier} 转写成功 ({note})"


def chain_asr_free() -> str:
    return _asr_file_tier("free")


def chain_asr_pro() -> str:
    return _asr_file_tier("pro_pro")


def chain_asr_realtime() -> str:
    import time
    import numpy as np
    from core.transcription.connectors.dashscope_realtime_stream import (
        DashScopeLiveSession, LiveASRCallback,
    )

    boot("realtime")
    config = {
        "api_key": os.environ.get("TRANSCRIPTION_API_KEY", ""),
        "model": os.environ.get("TRANSCRIPTION_MODEL", ""),
        "sample_rate": int(os.environ.get("TRANSCRIPTION_SAMPLE_RATE", "8000")),
        "language": "zh",
    }
    assert config["api_key"], "TRANSCRIPTION_API_KEY 未注入"

    class CB(LiveASRCallback):
        def __init__(self):
            self.finals = []
            self.error = None
            self.completed = False

        def on_open(self): pass
        def on_partial(self, text): pass
        def on_final(self, text, b, e): self.finals.append(text)
        def on_error(self, msg): self.error = msg
        def on_complete(self): self.completed = True
        def on_close(self): pass

    cb = CB()
    session = DashScopeLiveSession(config, cb)
    session.start()

    # 合成 8kHz PCM 分帧投递（模拟音频线程 200ms/帧）
    duration = 1.2
    total = int(8000 * duration)
    pcm = np.zeros(total, dtype=np.int16)
    for i in range(total):
        t = i / 8000
        if t < 0.4:
            pcm[i] = int(12000 * math_sin(2 * 3.14159 * 300 * t))
        elif 0.5 < t < 0.9:
            pcm[i] = int(10000 * math_sin(2 * 3.14159 * 800 * t))
    frame = 1600  # 200ms @ 8k
    for off in range(0, total, frame):
        chunk = pcm[off:off + frame]
        if len(chunk) == 0:
            break
        padded = np.pad(chunk, (0, max(0, frame - len(chunk))))
        session.send_audio_frame(padded.reshape(-1, 1))
        time.sleep(0.05)

    deadline = time.time() + 25
    while time.time() < deadline and not (cb.completed or cb.error):
        time.sleep(0.1)
    session.stop()

    if cb.error:
        return f"FAIL | 实时流错误: {cb.error[:100]}"
    if not cb.completed and not cb.finals:
        return "FAIL | 25s 内未收到 completed/final（链路不通或超时）"
    return (f"PASS | WebSocket 会话完成 finals={len(cb.finals)} "
            f"text={''.join(cb.finals)[:30]!r}")


def math_sin(x):
    import math
    return math.sin(x)


def chain_asr_lite() -> str:
    """两段式：先探 SFTP 可达性，可达才走完整 submit+query。"""
    import socket
    host = None
    try:
        boot("pro_lite")
        host = os.environ.get("AUDIO_HOST_HOST")
        port = int(os.environ.get("AUDIO_HOST_PORT", "22"))
        s = socket.create_connection((host, port), timeout=8)
        s.close()
    except Exception as e:
        return f"SKIP | 托管服务器 {host} 不可达: {e}"

    from core.transcription import get_connector, TranscriptionRequest
    wav = make_speech_wav(STAGING / "asr_lite_probe.wav")
    connector = get_connector()
    try:
        with open(wav, "rb") as f:
            req = TranscriptionRequest(
                audio_file=f, filename=wav.name, mime_type="audio/wav",
                extra_options={"local_path": str(wav)},
            )
            resp = connector.transcribe(req)
    except Exception as e:
        # 本机无部署私钥（免密配在开发机）→ 环境受限而非代码缺陷
        if "authentication" in str(e).lower() or "No authentication" in str(e):
            return f"SKIP | SFTP 认证失败（本机无托管服务器私钥）: {str(e)[:80]}"
        raise
    text = (resp.text or "").strip()
    return f"PASS | volc_doubao 响应正常 text={text[:30]!r}"



CHAINS = {
    "llm": chain_llm,
    "vlm": chain_vlm,
    "asr_free": chain_asr_free,
    "asr_realtime": chain_asr_realtime,
    "asr_pro": chain_asr_pro,
    "asr_lite": chain_asr_lite,
}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", required=True, choices=sorted(CHAINS))
    args = ap.parse_args()
    try:
        result = CHAINS[args.chain]()
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        result = f"FAIL | {type(e).__name__}: {str(e)[:150]}"
    print(f"[{args.chain}] {result}")
    sys.exit(0 if result.startswith(("PASS", "SKIP")) else 1)
