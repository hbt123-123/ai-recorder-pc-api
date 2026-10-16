"""SQLAlchemy 2.0 数据模型。

改造自 speakr：从 Flask-SQLAlchemy (db.Model / db.session) 迁移到原生
SQLAlchemy 2.0 (DeclarativeBase + sessionmaker)。

- Base: 所有模型的声明基类
- engine / SessionLocal: 延迟初始化，避免 import 时就读取配置
- init_engine(): 应用启动时显式初始化（可指定 database_url）
- get_session(): 获取新 session，调用方负责关闭
- init_db(): 建表

模型类在 Base 定义后导入，确保 `from core.models import Recording` 可用。
"""

from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session

from core.config import get_config


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""
    pass


_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None


def init_engine(database_url: Optional[str] = None) -> Engine:
    """初始化全局 engine 与 session 工厂。

    Args:
        database_url: SQLAlchemy URL；不传则从 core.config 读取。
    """
    global _engine, _SessionLocal
    url = database_url or get_config().database_url
    _engine = create_engine(url, future=True)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    """获取全局 engine，未初始化则用默认配置初始化。"""
    if _engine is None:
        init_engine()
    return _engine  # type: ignore[return-value]


def get_session() -> Session:
    """获取一个新的 session。调用方负责关闭（建议用 with 或 try/finally）。"""
    if _SessionLocal is None:
        init_engine()
    return _SessionLocal()  # type: ignore[misc]


def init_db() -> None:
    """创建所有表 + 幂等迁移会议模式新增列。

    确保模型子模块已导入以注册到 Base.metadata。新表由 create_all 建表；
    已有 SQLite 表需 ALTER TABLE ADD COLUMN 添加会议模式新增字段
   （SQLite 不支持 IF NOT EXISTS 语义，用 PRAGMA table_info 检查幂等）。
    """
    from .recording import Recording, TranscriptChunk, RecordingAudioSegment  # noqa: F401
    from .attachment import RecordingAttachment  # noqa: F401
    engine = get_engine()
    Base.metadata.create_all(engine)
    _migrate_meeting_columns(engine)


def _migrate_meeting_columns(engine) -> None:
    """幂等添加会议模式相关列（SQLite ALTER TABLE ADD COLUMN）。

    新增字段：
    - recordings.audio_source / recordings.meeting_mode
    - recording_attachments.source

    旧数据：audio_source 默认 None（按 mic 兜底）、meeting_mode 默认 False、
    source 默认 'camera'。无需数据回填脚本。

    维护说明：
    - 新增会议相关字段时,必须在此函数追加对应 ALTER TABLE 语句
    - 每条 ALTER 用 PRAGMA table_info 检查列存在性保证幂等(SQLite 不支持
      IF NOT EXISTS 语义)
    - 字段默认值需与 UserConfig dataclass 默认值一致
    - 当前机制为硬编码迁移(未引入 Alembic),适合字段少的项目阶段;字段增多后
      可考虑抽象为通用迁移注册机制
    """
    from sqlalchemy import text, inspect

    inspector = inspect(engine)

    # recordings 表新增列
    if "recordings" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("recordings")}
        with engine.begin() as conn:
            if "audio_source" not in existing:
                conn.execute(text(
                    "ALTER TABLE recordings ADD COLUMN audio_source VARCHAR(20)"
                ))
            if "meeting_mode" not in existing:
                conn.execute(text(
                    "ALTER TABLE recordings ADD COLUMN meeting_mode BOOLEAN DEFAULT 0"
                ))

    # recording_attachments 表新增列
    if "recording_attachments" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("recording_attachments")}
        if "source" not in existing:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE recording_attachments "
                    "ADD COLUMN source VARCHAR(20) DEFAULT 'camera'"
                ))


# 在 Base 定义后导入模型类，使其注册到 Base.metadata
from .recording import Recording, TranscriptChunk, RecordingAudioSegment  # noqa: E402
from .attachment import RecordingAttachment  # noqa: E402

__all__ = [
    "Base",
    "init_engine",
    "get_engine",
    "get_session",
    "init_db",
    "Recording",
    "TranscriptChunk",
    "RecordingAudioSegment",
    "RecordingAttachment",
]
