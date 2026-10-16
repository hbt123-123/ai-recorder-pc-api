"""
AI 录音应用 — 共享核心库 core/

PC 两版（pc_api / pc_local）共用的核心能力：
- transcription: 转写连接器（从 speakr 抽取，去 Flask 化）
- vlm: 视觉语言模型连接器（新建，复刻 transcription 模式）
- storage: 文件存储层（从 speakr 抽取，零改动复用 local 后端）
- models: SQLAlchemy 2.0 数据模型（精简版 + 附件表）
- audio: 音频录制（sounddevice）
- fusion: 多模态融合（照片 ↔ 转写时间轴对齐）
- config: 统一配置管理
"""

__version__ = "0.1.0"
