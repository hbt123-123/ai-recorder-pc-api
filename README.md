# AI 录音 · PC 云 API 版

本目录是 AI 录音项目的 **PC 云 API 版本**，完全独立、自包含，不依赖其他版本目录。

## 版本定位

| 特性 | 说明 |
|------|------|
| **形态** | PySide6 桌面应用（可 PyInstaller / Nuitka 打包为 exe） |
| **转写引擎** | 四级云 ASR tier（免费/实时/专业 Lite/专业 Pro） |
| **VLM 引擎** | 主用智谱 `GLM-4.6V-Flash`，失败自动切换备用百炼 `Qwen-VL-Max`（OpenAI 兼容，凭证硬编码） |
| **LLM 引擎** | 主用智谱 `GLM-4.7-Flash`，失败自动切换备用 DeepSeek `deepseek-v4-flash`（融合笔记生成） |
| **GUI** | PySide6 桌面界面（含悬浮窗、全局热键、屏幕截图） |
| **数据存储** | SQLite 本地数据库 + 本地文件系统 |

## 四级 ASR Tier

| Tier | 名称 | 模型 | 价格 | 验证状态 |
|------|------|------|------|---------|
| `free` | 免费版 | SiliconFlow FunAudioLLM/SenseVoiceSmall | 免费 | ✅ |
| `realtime` | 实时版 | 阿里云 paraformer-realtime-8k-v2 | 0.864 元/小时 | ✅ |
| `pro_lite` | 专业版 Lite | 火山豆包录音文件识别 2.0 | 0.77 元/小时 | ⏳（需开通 `volc.seedasr.auc` 资源） |
| `pro_pro` | 专业版 Pro | 阿里云 fun-asr-flash-2026-06-15 | 0.792 元/小时 | ✅ |

> 注：免费版原用 TeleAI/TeleSpeechASR，因 SiliconFlow 端订阅权限问题（403）已切换为 SenseVoiceSmall。SenseVoiceSmall 仅返回纯文本（无时间戳），VAD 分段转写时段起始时间戳由 VAD 状态机提供。

## 目录结构

```
ai-recorder-pc-api/
├── main.py                          # 入口
├── test_ai_chains.py                # AI 链路诊断脚本
├── build.spec                       # PyInstaller 打包配置
├── build_nuitka.bat                 # Nuitka 打包脚本
├── generate_icons.py                # 应用图标生成
├── requirements.txt                 # Python 依赖
├── .gitignore
├── README.md                        # 本文件
├── CLAUDE.md                        # AI 编码助手架构指引
├── assets/                          # 应用图标资源
├── core/                            # 共享核心库（独立副本）
│   ├── transcription/               # 转写连接器（四级 tier）
│   │   └── connectors/              # 各厂商 ASR 连接器实现
│   ├── vlm/                         # VLM 连接器（智谱 + 百炼）
│   ├── fusion/                      # 多模态融合（时间轴对齐 + 笔记融合）
│   ├── storage/                     # 文件存储（local/s3 facade）
│   ├── audio/                       # 录音引擎（sounddevice + VAD 分段）
│   └── models/                      # 数据模型（SQLAlchemy 2.0）
├── pc_api/                          # PC API 版业务包
│   ├── recording_service.py         # 录音编排服务层
│   ├── camera.py                    # 摄像头拍照封装
│   ├── screen_capture.py            # 屏幕截图封装
│   ├── global_hotkey.py             # 全局热键
│   ├── config_loader.py             # 配置注入（UserConfig → 环境变量）
│   ├── credentials.py               # 凭证读取（从 .env 加载，无硬编码）
│   ├── gui/                         # PySide6 界面组件（主窗口/悬浮窗/pages/...）
│   └── workers/                     # QThread 后台任务
├── tests/                           # 单元测试（音频逻辑）
└── data/                            # 运行时数据（DB / 录音 / 暂存，gitignored）
```

## 安装与运行

```bash
# 1. 创建虚拟环境（推荐）
python -m venv myenv
# Windows
myenv\Scripts\activate
# macOS/Linux
source myenv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置凭证
cp .env.example .env
# 编辑 .env 填写各厂商 API Key
# （.env 已 gitignored，不会提交）

# 4. 运行（必须在本目录下执行）
python main.py
```

首次启动会弹出 OnboardingDialog 引导选择转写模型档位（四级 tier）；VLM 与 LLM 均硬编码主备双引擎，无需用户配置。

## 凭证管理

采用**双源配置模型**：

| 配置来源 | 用途 | 配置方式 |
|---------|------|---------|
| `.env`（由 `pc_api/credentials.py` 加载） | ASR/VLM/LLM 密钥、音频托管服务器 | 开发者填写（gitignored） |
| `~/.airecorder/config.json` | 用户运行时配置（tier、转写语言） | SettingsDialog 填写 |

`.env` 需包含以下字段（参考 `.env.example`）：

| 字段 | 用途 |
|------|------|
| `SILICONFLOW_API_KEY` | 免费版（SiliconFlow SenseVoiceSmall） |
| `BIGMODEL_API_KEY` / `BIGMODEL_BASE_URL` / `BIGMODEL_VLM_MODEL` / `BIGMODEL_LLM_MODEL` | **主用** VLM（glm-4.6v-flash）与 LLM（glm-4.7-flash），智谱 BigModel |
| `DASHSCOPE_API_KEY` | 实时版 + 专业版 Pro 转写；VLM 备用通道（Qwen-VL-Max） |
| `DEEPSEEK_API_KEY` | LLM 备用通道（deepseek-v4-flash） |
| `VOLC_APP_ID` / `VOLC_ACCESS_TOKEN` | 专业版 Lite（火山豆包，Token 认证；`VOLC_SECRET_KEY` 当前代码未使用，可选保留） |
| `AUDIO_HOST_HOST` / `AUDIO_HOST_PORT` / `AUDIO_HOST_USER` / `AUDIO_HOST_REMOTE_DIR` / `AUDIO_HOST_PUBLIC_BASE` | 音频托管服务器（SFTP 上传 + HTTP 暴露，认证走 SSH 密钥免密） |

## AI 链路诊断

```bash
python test_ai_chains.py
```

依赖：`.env` 已配置、`data/staging/` 下有测试素材。

## 运行时数据

- 录音文件：`./data/storage/`
- 临时文件：`./data/staging/`
- 数据库：`./data/airecorder.db`（SQLite）
- 用户配置：`~/.airecorder/config.json`（跨版本共享）

## 已知限制

- `pro_pro`：单音频 ≤5 分钟（fun-asr-flash-2026-06-15 同步接口限制）
- `pro_lite`：需在火山引擎控制台开通 `volc.seedasr.auc` 资源
- `pro_lite`：依赖音频托管服务器在线——nginx 暴露 `/airecorder/recordings/` 目录，且服务器防火墙须放行 80/tcp（豆包服务端经公网 URL 拉取音频）
- 音频托管服务器 SSH 认证依赖本机已配置的密钥（无密码字段）

## 架构文档

详见根目录 `CLAUDE.md`，包含：
- 双层包结构（core / pc_api）与双源配置注入机制
- 四级 ASR tier 定义与运行时热切换
- 录音流水线与线程模型
- 存储抽象与 pro_lite 外部依赖说明
