"""Factory for configuring file storage backends.

改造自 speakr：
- 第 33 行原 `from src.config import app_config` 改为直接读 os.environ，
  避免引入 Flask app_config 依赖。
- 去掉 `from .s3 import S3StorageBackend`（boto3 依赖）；MVP 仅支持 local，
  `build_s3_backend` 在配置了 S3 时抛 NotImplementedError，否则返回 None。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .local import LocalStorageBackend


@dataclass
class StorageSettings:
    backend: str
    local_root: str
    key_prefix: str
    staging_dir: str
    presign_ttl_seconds: int = 3600
    presign_public_ttl_seconds: int = 3600
    # S3 字段保留（MVP 不用，保持结构兼容以便后续扩展）
    s3_bucket_name: Optional[str] = None
    s3_region: Optional[str] = None
    s3_endpoint_url: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_access_key: Optional[str] = None
    s3_session_token: Optional[str] = None
    s3_use_path_style: bool = False
    s3_verify_ssl: bool = True


def _env_int(name: str, default: int) -> int:
    """读整型环境变量；非法值给出含变量名的清晰错误。"""
    raw = (os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(
            f"环境变量 {name} 的值 {raw!r} 不是合法整数（默认应为 {default}）"
        ) from e


def load_storage_settings_from_env() -> StorageSettings:
    """从环境变量加载存储配置（core/ 版本，去 app_config 依赖）。"""
    bucket = os.environ.get("S3_BUCKET_NAME") or None
    # presign TTL 仅在真正配置了 S3 时解析：纯 local 部署不该被无关的
    # 非法 S3 环境变量阻断启动
    parse_ttl = _env_int if bucket else (lambda name, default: default)
    return StorageSettings(
        backend=(os.environ.get("FILE_STORAGE_BACKEND", "local") or "local").strip().lower() or "local",
        local_root=os.environ.get("STORAGE_LOCAL_ROOT", "./data/storage"),
        key_prefix=(os.environ.get("FILE_STORAGE_KEY_PREFIX", "recordings") or "recordings").strip().strip("/").replace("\\", "/"),
        staging_dir=os.environ.get("FILE_STORAGE_STAGING_DIR", "./data/staging"),
        presign_ttl_seconds=parse_ttl("S3_PRESIGN_TTL_SECONDS", 3600),
        presign_public_ttl_seconds=parse_ttl("S3_PRESIGN_PUBLIC_TTL_SECONDS", 3600),
        s3_bucket_name=bucket,
        s3_region=os.environ.get("S3_REGION") or None,
        s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
        s3_access_key_id=os.environ.get("S3_ACCESS_KEY_ID") or None,
        s3_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY") or None,
        s3_session_token=os.environ.get("S3_SESSION_TOKEN") or None,
        s3_use_path_style=os.environ.get("S3_USE_PATH_STYLE", "false").lower() == "true",
        s3_verify_ssl=os.environ.get("S3_VERIFY_SSL", "true").lower() != "false",
    )


def build_local_backend(settings: StorageSettings) -> LocalStorageBackend:
    return LocalStorageBackend(settings.local_root)


def build_s3_backend(settings: StorageSettings) -> None:
    """MVP 不支持 S3 后端。

    未配置 S3 bucket 时返回 None（与原 speakr 行为一致，service 层据此判断）；
    配置了 S3 bucket 时抛 NotImplementedError，提示 S3 后端未在 core/ 启用。
    注：本函数要么抛异常要么返回 None，永远不会构造后端实例。
    """
    if not settings.s3_bucket_name:
        return None
    raise NotImplementedError(
        "S3 storage backend is not supported in core/ MVP. "
        "Set FILE_STORAGE_BACKEND=local."
    )
