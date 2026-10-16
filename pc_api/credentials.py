"""凭证读取模块：所有第三方 ASR / VLM / LLM / 服务器托管密钥集中从 .env 加载。

密钥定义在仓库根目录 .env（已 gitignore，勿提交），模板见 .env.example：
    cp .env.example .env   # 填入真实值

本模块仅做 load_dotenv + os.environ 读取，不含任何硬编码密钥，可安全提交。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 显式定位仓库根目录的 .env（不依赖 cwd，保证任意入口脚本都能加载）
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")


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
