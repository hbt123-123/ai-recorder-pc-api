# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

AI 录音 · **PC 云 API 版**：PySide6 桌面应用，录音 + 实时拍照 + 云 ASR 转写 + VLM 图片描述 + LLM 图文融合笔记。自包含、独立运行，是「AI 录音」三版本（PC API / PC 本地 / 安卓）之一。本仓库仅含 PC API 版。

VLM 固定阿里云百炼 `qwen-vl-max`、LLM 固定 DeepSeek `deepseek-v4-flash`，均硬编码、OpenAI 兼容；用户唯一需选择的只是 ASR 档位。

## 常用命令

```bash
# 必须在本仓库根目录执行（main.py 用 sys.path.insert + 相对路径 ./data/）
python main.py

# 虚拟环境（Windows）
myenv/Scripts/activate
pip install -r requirements.txt

# 配置凭证（.env 已 gitignored）
cp .env.example .env   # 填入真实 API key

# 云 API 连通性诊断（不启 GUI，直调 core 连接器；需 ~/.airecorder/config.json + .env + data/staging 测试素材）
python test_cloud_api.py

# pro_pro tier 端到端验证
python test_funasr_e2e.py
```

无 pytest 测试套件——`test_*.py` 是独立诊断脚本，依赖 `data/staging/` 下的现成 wav/jpg 素材与已配置的 `~/.airecorder/config.json`。

## 架构

### 双层包结构

- **`core/`** —— 引擎无关共享核心库（PC API 与 PC 本地版共用同一份契约）。所有配置**只从环境变量读**（`core/config.py` 单例），不含任何 GUI/业务逻辑。子系统：`transcription/`（连接器 + registry + 四级 tier 定义）、`vlm/`、`fusion/`（时间轴对齐 + 笔记融合）、`storage/`（local/s3 facade + locator 抽象）、`audio/`（sounddevice 录音）、`models/`（SQLAlchemy 2.0）。
- **`pc_api/`** —— PC API 版业务包。`recording_service.py`（编排层，隔离 GUI 与 core）、`config_loader.py`（配置注入）、`gui/`（PySide6）、`workers/`（QThread 后台任务）、`credentials.py`（从 .env 读取密钥）。

### 双源配置 + 注入机制（核心，跨多文件）

配置来自两个源，由 `pc_api/config_loader.py::apply_config()` 桥接：

| 源 | 内容 | 形式 |
|----|------|------|
| `.env`（由 `pc_api/credentials.py` 加载） | ASR/VLM/LLM/SFTP 密钥 | dotenv 键值对（gitignored） |
| `~/.airecorder/config.json` | 用户运行时选择（`asr_tier`、`language`、VLM/LLM 设置） | `core/user_config.py::UserConfig` |

`apply_config(cfg)` 流程：按 `cfg.asr_tier` 选 tier → 注入对应连接器的环境变量（`TRANSCRIPTION_CONNECTOR`/`TRANSCRIPTION_API_KEY`/`TRANSCRIPTION_MODEL` 等，见 `core/transcription/tiers.py` 的 tier→connector 映射）+ VLM/LLM/存储 env → **重置全部 core 单例**使新 env 生效：

```python
reset_config()                          # core/config 单例
reset_storage_service_singleton()       # core/storage/service 单例
init_engine()                           # SQLAlchemy engine + sessionmaker
get_registry().reinitialize()           # 转写连接器
```

**改 env 后必须重置这四个单例**，否则旧连接器/旧 engine 仍在用旧配置。任何新增「运行时可切换」的配置都要走这条路。

### 四级 ASR tier + 运行时切换

`core/transcription/tiers.py` 集中定义四档（`free`/`realtime`/`pro_lite`/`pro_pro`），每档绑定 connector + model + 价格，UI 与 config_loader 共用同一份 `TIER_DEFS`。连接器注册在 `core/transcription/registry.py::ConnectorRegistry` 单例，按 `TRANSCRIPTION_CONNECTOR` env 自动初始化活动连接器：

| tier | connector | 说明 |
|------|-----------|------|
| free | `openai_whisper` | 硅基流动 TeleSpeechASR（OpenAI 兼容） |
| realtime | `dashscope_realtime` / `dashscope_realtime_stream` | 阿里 paraformer-realtime-8k-v2，WebSocket 实时流 |
| pro_lite | `volc_doubao_file` | 火山豆包，submit+query，**音频需 SFTP 托管** |
| pro_pro | `dashscope_funasr` | 阿里 fun-asr-flash，同步 REST，≤5min |

侧边栏点击即切换 tier：`MainWindow._switch_to_tier()` → 写回 `config.json` + `apply_config()` 重新注入 env + 重置单例。`MainWindow` 懒加载 tier 页面（首次切换才构造）。

### 转写两条路径（`gui/pages/base_page.py`）

- **批处理**（free/pro_lite/pro_pro）：录音停止后点「开始转写」→ `TranscriptionWorker` 读 audio_path → 调 `get_connector().transcribe()` → 落库。
- **实时流**（realtime）：录音开始即 `RealtimeTranscriptionWorker` 订阅 `AudioRecorder` 音频帧 → 喂 `DashScopeLiveSession` → partial/final 信号实时回 GUI。停止时 join SDK 线程、拼装 `TranscriptionResponse` 落库。**失败回退**：`_on_realtime_failed` 显露「开始转写」按钮，改走文件转写，录音不受影响。

两条路径在 `_after_transcription_ready()` 统一收尾（展示文本 → 启动 VLM 或启用笔记按钮）。

### 录音流水线

`RecordingService` 编排：`create_recording` → `start_recording` →（录音中拍照存附件）→ `stop_recording` → `store_audio` → 转写 worker → `save_transcription`（纯文本入 `Recording.transcription`，segments 入 `TranscriptChunk`）→ VLM worker → `save_attachment_description` → `NoteFuser.generate_note` → 存 `Recording.summary`。

### 线程模型

- GUI 主线程；QThread workers（转写/VLM/笔记/实时）通过 Qt 信号 `QueuedConnection` 回主线程。
- sounddevice **音频线程**（`AudioRecorder._callback`）：写 WAV + 算音量 + 通知订阅者，必须非阻塞。
- dashscope SDK **内部线程**（实时回调）：仅 emit 信号 + append 列表。
- `RecordingService` 每个方法创建**独立 SQLAlchemy session** 并 `expunge` 对象后返回，保证线程安全。

### 存储抽象

`StorageService`（`core/storage/service.py`）是 local/s3 后端 facade，用 `locator` 字符串抽象文件位置。读音频用 `materialize(locator)` contextmanager。默认 local 后端，根目录 `./data/storage`，暂存 `./data/staging`，DB `./data/airecorder.db`。

### pro_lite 的外部依赖

火山豆包服务端需用公网 HTTP URL 拉取音频（无法解析 DashScope 的 `oss://`），故 `core/transcription/audio_hosting.py` 用 paramiko SFTP 把本地 wav 上传到公网服务器（`8.130.23.56`，SSH 免密已配置），返回 `http://8.130.23.56/airecorder/recordings/<uuid>.wav`。该 tier 依赖此服务器在线 + nginx 暴露目录（配置见 `.trae/documents/nginx_airecorder.conf`）。

## 关键约定

- **运行目录**：必须在仓库根目录执行 `python main.py`（相对路径 `./data/` 依赖 cwd）。
- **凭证**：所有密钥集中在仓库根目录 `.env`（gitignored），由 `pc_api/credentials.py` 用 python-dotenv 加载；新增厂商 key 加到 `.env.example` 模板与 `apply_config()` 注入逻辑。
- **音频格式**：统一 16kHz / 16-bit / mono WAV（`AudioRecorder` 常量）。
- **延迟导入**：`sounddevice`、`paramiko`、`cv2` 在方法内延迟导入，避免无硬件/未装依赖时 `import` 失败。
- **注释/文档串一律中文**，与现有代码风格一致。
- 架构规划文档在 `.trae/documents/`（`三版本AI录音应用架构规划.md`、`实施路线图.md`）。

## 项目清理记录

**日期**：2026-07-26

**删除类别与原因**：
- **项目源码树 `__pycache__/`**（14 个目录）：Python 字节码缓存，纯衍生产物，由源 `.py` 编译生成，运行时自动重建。已 gitignored。
- **`data/staging/rec_*.wav`**（3 个文件，约 2.4 MB）：录音 staging 副本，`RecordingService.store_audio()` 已将字节级一致的副本写入 `data/storage/recordings/YYYY/MM/DD/`，staging 端为冗余残留。删除前用 SHA256 校验 staging 与 storage 副本完全一致。

**保留**：`data/staging/test.wav`（被 `test_funasr_e2e.py` 引用）；`myenv/`（虚拟环境，重建成本高）；`.env`（API key）；`data/airecorder.db` 与 `data/storage/recordings/`（用户数据）；3 个根目录 `test_*.py`（活跃测试）；`.trae/documents/` 设计/部署文档。

**再生机制**：`__pycache__/` 在下次运行 `python main.py` 或 `python test_vad.py` 时由 CPython 自动重建，无需任何手动操作。
