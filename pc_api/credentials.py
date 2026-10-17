"""凭证读取模块：所有第三方 ASR / VLM / LLM / 服务器托管密钥集中从 .env 加载。

.env 位置（按优先级依次加载；load_dotenv 默认不覆盖已有环境变量，多文件只会互补缺失项）：
- 开发态：仓库根目录 .env（已 gitignore，勿提交），模板见 .env.example：
      cp .env.example .env   # 填入真实值
- 打包产物（PyInstaller / Nuitka）：优先 exe 同目录 .env（分发时放这里即可），
  再兜底打包资源目录（PyInstaller onedir 为 _internal\\.env，历史位置）。

本模块仅做 load_dotenv + os.environ 读取，不含任何硬编码密钥，可安全提交。
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _env_candidates() -> list[Path]:
    """按优先级返回候选 .env 路径（不依赖 cwd，保证任意入口脚本都能加载）。"""
    if getattr(sys, "frozen", False):
        # 冻结态：exe 同目录最直观（用户解压后直接把 .env 放在 exe 旁）
        candidates = [Path(sys.executable).resolve().parent / ".env"]
        # 兜底：打包资源目录。PyInstaller onedir 下本文件位于 _internal 内，
        # parent.parent 即 _MEIPASS；onefile 下为临时解包目录（仅作兼容）。
        bundled = Path(__file__).resolve().parent.parent / ".env"
        if bundled not in candidates:
            candidates.append(bundled)
        return candidates
    return [Path(__file__).resolve().parent.parent / ".env"]


for _candidate in _env_candidates():
    load_dotenv(_candidate)


def _require(name: str) -> str:
    """读取必需密钥，缺失时给出明确报错而非静默空串。"""
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"缺少环境变量 {name}：请在仓库根目录 .env 中配置（参考 .env.example）"
        )
    return value


# ----- 免费版：硅基流动 SenseVoiceSmall（OpenAI 兼容）-----
SILICONFLOW_API_KEY = _require("SILICONFLOW_API_KEY")

# ----- 主用模型：智谱 BigModel（VLM glm-4.6v-flash + LLM glm-4.7-flash）-----
BIGMODEL_API_KEY = _require("BIGMODEL_API_KEY")
BIGMODEL_BASE_URL = os.environ.get("BIGMODEL_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
BIGMODEL_VLM_MODEL = os.environ.get("BIGMODEL_VLM_MODEL", "glm-4.6v-flash")
BIGMODEL_LLM_MODEL = os.environ.get("BIGMODEL_LLM_MODEL", "glm-4.7-flash")

# ----- 阿里云百炼（实时版 + 专业版 Pro + VLM 备用 Qwen-VL-Max 共用）-----
DASHSCOPE_API_KEY = _require("DASHSCOPE_API_KEY")

# ----- 备用文本整理 LLM：DeepSeek -----
DEEPSEEK_API_KEY = _require("DEEPSEEK_API_KEY")

# ----- 专业版 Lite：火山引擎豆包录音文件识别 2.0 -----
VOLC_APP_ID = _require("VOLC_APP_ID")
VOLC_ACCESS_TOKEN = _require("VOLC_ACCESS_TOKEN")
VOLC_SECRET_KEY = _require("VOLC_SECRET_KEY")

# ----- 服务器音频托管（火山 Lite 版用：SFTP 上传后 HTTP 暴露）-----
AUDIO_HOST_HOST = os.environ.get("AUDIO_HOST_HOST", "8.130.23.56")
AUDIO_HOST_PORT = int(os.environ.get("AUDIO_HOST_PORT", "22"))
AUDIO_HOST_USER = os.environ.get("AUDIO_HOST_USER", "root")
AUDIO_HOST_REMOTE_DIR = os.environ.get("AUDIO_HOST_REMOTE_DIR", "/var/www/airecorder/recordings")
AUDIO_HOST_PUBLIC_BASE = os.environ.get(
    "AUDIO_HOST_PUBLIC_BASE", "http://8.130.23.56/airecorder/recordings"
)
