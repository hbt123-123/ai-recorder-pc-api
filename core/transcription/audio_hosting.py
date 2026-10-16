"""服务器音频托管助手：把本地 wav 经 SFTP 上传到公网服务器，返回 HTTP URL。

供专业版 Lite（火山豆包录音文件识别）连接器使用——火山服务端需用 HTTPS/HTTP
公网 URL 下载音频，DashScope 上传的 oss:// 不可被火山解析，故改走自有服务器。

依赖 paramiko；SSH 免密登录须预先配置（默认读取 ~/.ssh/id_rsa）。
配置从环境变量读取（由 pc_api/config_loader 按 tier 注入）。

安全说明：
- 主机密钥优先取 ~/.ssh/known_hosts 已记录指纹；首次连接的新主机记录
  指纹并告警日志后接受（桌面单用户场景的折中），后续连接与 known_hosts
  不符会直接拒绝，可发现中间人替换。
- 托管的语音文件是隐私数据，转写完成后应调用 delete_hosted_file() 清理。
"""

import logging
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# SFTP 连接超时（秒）：TCP 建连 / banner / 认证各阶段共用量级
_SSH_TIMEOUT = 15


def _cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _first_seen_host_key_policy():
    """构造"未知主机密钥记录指纹后接受"的策略实例。

    首次连接服务器时 known_hosts 无记录属正常场景；直接 AutoAddPolicy 会
    静默接受任意密钥（MITM 无感知）。此处显式打印指纹留痕，已知主机不匹配
    时 paramiko 底层仍会抛 BadHostKeyException 拒绝。
    需在 paramiko 可导入的运行环境中调用（延迟导入约定）。
    """
    import paramiko

    class _FirstSeenHostKeyPolicy(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            fingerprint = ":".join(f"{b:02x}" for b in key.get_fingerprint())
            logger.warning(
                f"首次连接主机 {hostname}，记录主机密钥指纹 "
                f"({key.get_name()}): {fingerprint}"
            )
            client.get_host_keys().add(hostname, key.get_name(), key)

    return _FirstSeenHostKeyPolicy()


def upload_and_get_url(local_path: str) -> str:
    """SFTP 上传本地音频到服务器，返回公网可访问的 HTTP URL。

    Args:
        local_path: 本地 wav 文件路径

    Returns:
        形如 http://8.130.23.56/airecorder/recordings/<uuid>.wav 的 URL
    """
    try:
        import paramiko
    except ImportError as e:
        raise RuntimeError("缺少 paramiko，请 pip install paramiko") from e

    host = _cfg("AUDIO_HOST_HOST")
    port = int(_cfg("AUDIO_HOST_PORT", "22"))
    user = _cfg("AUDIO_HOST_USER", "root")
    remote_dir = _cfg("AUDIO_HOST_REMOTE_DIR", "/var/www/airecorder/recordings")
    public_base = _cfg("AUDIO_HOST_PUBLIC_BASE", "").rstrip("/")

    if not host:
        raise ValueError("AUDIO_HOST_HOST 未配置（火山 Lite 版需要服务器托管音频）")
    if not public_base:
        # 缺失时会拼出相对路径被当作 URL 返回，火山拉取失败且难定位根因
        raise ValueError("AUDIO_HOST_PUBLIC_BASE 未配置（无法生成可公网访问的音频 URL）")
    if not os.path.exists(local_path):
        raise FileNotFoundError(local_path)

    # 用 uuid 避免文件名冲突，保留原扩展名
    ext = Path(local_path).suffix or ".wav"
    remote_name = f"{uuid.uuid4().hex}{ext}"
    remote_path = f"{remote_dir.rstrip('/')}/{remote_name}"

    pkey = _load_default_private_key()

    client = paramiko.SSHClient()
    # 已知主机指纹来自 ~/.ssh/known_hosts；不在其中的新主机走下方策略
    client.load_system_host_keys()
    client.set_missing_host_key_policy(_first_seen_host_key_policy())
    try:
        logger.info(f"SFTP 连接 {user}@{host}:{port}")
        client.connect(
            host,
            port=port,
            username=user,
            pkey=pkey,
            timeout=_SSH_TIMEOUT,
            banner_timeout=_SSH_TIMEOUT,
            auth_timeout=_SSH_TIMEOUT,
            allow_agent=True,
            look_for_keys=True,
        )
        sftp = client.open_sftp()

        # 确保远程目录存在
        _ensure_remote_dir(sftp, remote_dir)

        logger.info(f"SFTP 上传 {local_path} → {remote_path}")
        sftp.put(local_path, remote_path)
        sftp.close()
    finally:
        client.close()

    url = f"{public_base}/{remote_name}"
    logger.info(f"服务器托管 URL：{url}")
    return url


def delete_hosted_file(public_url: str) -> bool:
    """尽力删除已托管的远程音频文件（转写完成后调用，避免语音长期暴露）。

    Args:
        public_url: upload_and_get_url 返回的 HTTP URL

    Returns:
        删除成功返回 True；任何失败（网络/权限/URL 无法解析）仅记日志
        并返回 False，绝不向调用方抛异常——清理失败不应影响主流程结果。
    """
    try:
        import paramiko
    except ImportError:
        logger.warning("缺少 paramiko，跳过托管音频清理")
        return False

    host = _cfg("AUDIO_HOST_HOST")
    remote_dir = _cfg("AUDIO_HOST_REMOTE_DIR", "/var/www/airecorder/recordings")

    parsed = urlparse(public_url)
    remote_name = Path(parsed.path).name
    if not host or not remote_name:
        logger.warning(f"托管音频清理参数不足，跳过: url={public_url!r}")
        return False
    remote_path = f"{remote_dir.rstrip('/')}/{remote_name}"

    # port 解析与私钥加载同样纳入契约：任何前置失败仅记日志返回 False，
    # 清理是尽力而为的收尾动作，绝不让异常打断调用方主流程
    client = None
    sftp = None
    try:
        port = int(_cfg("AUDIO_HOST_PORT", "22"))
        user = _cfg("AUDIO_HOST_USER", "root")
        pkey = _load_default_private_key()
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(_first_seen_host_key_policy())
        client.connect(
            host,
            port=port,
            username=user,
            pkey=pkey,
            timeout=_SSH_TIMEOUT,
            banner_timeout=_SSH_TIMEOUT,
            auth_timeout=_SSH_TIMEOUT,
            allow_agent=True,
            look_for_keys=True,
        )
        sftp = client.open_sftp()
        sftp.remove(remote_path)
        logger.info(f"已删除托管音频: {remote_path}")
        return True
    except Exception as e:
        logger.warning(f"托管音频清理失败（不影响转写结果）: {e}")
        return False
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        if client is not None:
            client.close()


def _ensure_remote_dir(sftp, remote_dir: str) -> None:
    """递归创建远程目录（若不存在）。"""
    parts = [p for p in remote_dir.split("/") if p]
    cur = ""
    for p in parts:
        cur = f"{cur}/{p}"
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


def _load_default_private_key():
    """加载默认 SSH 私钥（与 OpenSSH 客户端一致的查找顺序）。

    依次尝试 ~/.ssh/id_rsa / id_ed25519 / id_ecdsa / id_dsa。
    返回 paramiko PKey 或 None（若均不存在或格式不支持）。
    加载失败时记录具体原因：带口令私钥静默跳过会让用户只看到
    莫名的 AuthenticationException，无法定位根因。
    """
    import paramiko

    ssh_dir = Path.home() / ".ssh"
    candidates = ["id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"]
    # 收集可用的密钥加载器（新 paramiko 可能移除 DSSKey）
    loaders = []
    for attr in ("RSAKey", "Ed25519Key", "ECDSAKey", "DSSKey"):
        loader = getattr(paramiko, attr, None)
        if loader is not None:
            loaders.append(loader)

    for name in candidates:
        key_path = ssh_dir / name
        if not key_path.exists():
            continue
        for loader in loaders:
            try:
                return loader.from_private_key_file(str(key_path))
            except paramiko.PasswordRequiredException:
                logger.warning(
                    f"私钥 {key_path} 受口令保护，免密交互场景无法使用"
                )
                break
            except paramiko.SSHException as e:
                logger.warning(f"私钥 {key_path} 加载失败（{loader.__name__}）: {e}")
                continue
    return None
