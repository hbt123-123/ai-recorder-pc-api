"""火山引擎 豆包录音文件识别模型 2.0 连接器（专业版 Lite）。

基于 https://docs.volcengine.com/docs/6561/1354868 标准版 HTTP API。
submit + query 两阶段：提交音频 URL → 轮询任务结果。

鉴权（旧版控制台，对应 .env 的 APP_ID + Access_Token）：
    X-Api-App-Key      = APP_ID
    X-Api-Access-Key   = Access Token
    X-Api-Resource-Id   = volc.seedasr.auc（豆包录音文件识别 2.0）
    X-Api-Request-Id    = 任务 ID（UUID，submit 与 query 须一致）
    X-Api-Sequence      = -1（仅 submit）

音频需公网可下载 URL：本地 wav 经 audio_hosting SFTP 上传到 8.130.23.56 后 HTTP 暴露。
价格：0.77 元/小时。
"""

import logging
import time
import uuid
from typing import Any, Dict, List, Set

import httpx

from ..audio_hosting import delete_hosted_file, upload_and_get_url
from ..base import (
    BaseTranscriptionConnector,
    ConnectorSpecifications,
    TranscriptionCapability,
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionSegment,
)
from ..exceptions import ConfigurationError, TranscriptionError

logger = logging.getLogger(__name__)

_RESOURCE_ID_DOUBAO_2 = "volc.seedasr.auc"
_SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
_QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"

# 状态码（X-Api-Status-Code 头）
_STATUS_SUCCESS = 20000000
_STATUS_PROCESSING = 20000001
_STATUS_QUEUED = 20000002

# 轮询参数
_POLL_INTERVAL = 3.0          # 每次查询间隔秒
_POLL_TIMEOUT = 30 * 60       # 最长等待 30 分钟
_MAX_CONSECUTIVE_ERRORS = 20  # 连续网络错误上限（约 1 分钟），超过判定链路故障


class VolcDoubaoFileConnector(BaseTranscriptionConnector):
    """火山豆包录音文件识别 2.0 连接器。"""

    CAPABILITIES: Set[TranscriptionCapability] = {
        TranscriptionCapability.TIMESTAMPS,
        TranscriptionCapability.LANGUAGE_DETECTION,
    }
    PROVIDER_NAME = "volc_doubao_file"
    SPECIFICATIONS = ConnectorSpecifications(
        max_file_size_bytes=512 * 1024 * 1024,  # 512MB
        max_duration_seconds=None,
        handles_chunking_internally=True,
    )

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.app_id = config["app_id"]
        self.access_token = config["access_token"]
        self.resource_id = config.get("resource_id", _RESOURCE_ID_DOUBAO_2)
        self.submit_url = config.get("submit_url", _SUBMIT_URL)
        self.query_url = config.get("query_url", _QUERY_URL)

    def _validate_config(self) -> None:
        if not self.config.get("app_id"):
            raise ConfigurationError("app_id is required for volc_doubao_file connector")
        if not self.config.get("access_token"):
            raise ConfigurationError("access_token is required for volc_doubao_file connector")

    def _headers(self, task_id: str, with_sequence: bool = False) -> Dict[str, str]:
        h = {
            "X-Api-App-Key": self.app_id,
            "X-Api-Access-Key": self.access_token,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": task_id,
            "Content-Type": "application/json",
        }
        if with_sequence:
            h["X-Api-Sequence"] = "-1"
        return h

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        try:
            # 1. 取本地路径并上传到公网服务器
            local_path = (request.extra_options or {}).get("local_path")
            if not local_path:
                raise TranscriptionError("volc_doubao_file 需要 extra_options['local_path']")
            logger.info("volc_doubao_file: 上传音频到服务器…")
            audio_url = upload_and_get_url(local_path)

            # 2. 生成 task_id 并提交
            task_id = uuid.uuid4().hex
            submit_body = {
                "user": {"uid": "airecorder"},
                "audio": {
                    "format": "wav",
                    "url": audio_url,
                    "rate": 16000,
                    "channel": 1,
                },
                "request": {
                    "model_name": "bigmodel",
                    "enable_itn": True,
                    "enable_punc": True,
                    "show_utterances": True,
                },
            }
            if request.language:
                submit_body["audio"]["language"] = request.language

            logger.info(f"volc_doubao_file: 提交任务 task_id={task_id} url={audio_url}")
            # 2-4 全程纳入 try/finally：无论成功、提交失败、轮询失败还是
            # 超时，都统一清理托管音频——失败路径若不清理，重试会不断向
            # 无鉴权公网目录累积新 UUID 语音文件，泄露面单调扩大
            try:
                with httpx.Client(timeout=120.0) as client:
                    resp = client.post(
                        self.submit_url, headers=self._headers(task_id, with_sequence=True),
                        json=submit_body,
                    )
                try:
                    status_code = int(resp.headers.get("X-Api-Status-Code", "0"))
                except ValueError:
                    status_code = 0
                status_msg = resp.headers.get("X-Api-Message", "")
                if status_code != _STATUS_SUCCESS:
                    # 提交阶段非成功即失败（处理中状态出现在 query 阶段）
                    raise TranscriptionError(
                        f"火山 submit 失败 status={status_code} msg={status_msg} body={resp.text[:300]}"
                    )
                logger.info("volc_doubao_file: 提交成功，开始轮询…")

                # 3. 轮询查询
                result = self._poll_query(task_id)

                # 4. 解析结果
                return self._parse_result(result, task_id)
            finally:
                # 删除前记录 URL，保留排查线索
                logger.info(f"volc_doubao_file: 清理托管音频 {audio_url}")
                try:
                    delete_hosted_file(audio_url)
                except Exception as e:
                    logger.warning(f"volc_doubao_file: 托管音频清理异常: {e}")

        except TranscriptionError:
            raise
        except Exception as e:
            logger.error(f"volc_doubao_file 转写失败: {e}")
            raise TranscriptionError(f"火山豆包文件识别失败: {e}") from e

    def _poll_query(self, task_id: str) -> Dict[str, Any]:
        deadline = time.time() + _POLL_TIMEOUT
        consecutive_errors = 0
        with httpx.Client(timeout=60.0) as client:
            while time.time() < deadline:
                # 瞬时网络错误（超时/连接抖动）不应终止整个轮询窗口：
                # 服务端任务仍在执行并已计费，客户端丢弃结果代价更高。
                try:
                    resp = client.post(
                        self.query_url, headers=self._headers(task_id, with_sequence=False),
                        json={},
                    )
                except httpx.HTTPError as e:
                    consecutive_errors += 1
                    logger.warning(
                        f"volc_doubao_file: query 网络错误({consecutive_errors}"
                        f"/{_MAX_CONSECUTIVE_ERRORS})，继续轮询: {e}"
                    )
                    if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                        raise TranscriptionError(
                            f"火山 query 连续网络错误 {consecutive_errors} 次，判定链路故障"
                        ) from e
                    time.sleep(_POLL_INTERVAL)
                    continue
                consecutive_errors = 0

                try:
                    status_code = int(resp.headers.get("X-Api-Status-Code", "0"))
                except ValueError:
                    status_code = 0
                status_msg = resp.headers.get("X-Api-Message", "")

                if status_code == _STATUS_SUCCESS:
                    logger.info(f"volc_doubao_file: 识别完成 task_id={task_id}")
                    try:
                        return resp.json()
                    except Exception as e:
                        raise TranscriptionError(
                            f"火山 query 返回非 JSON: {resp.text[:300]}"
                        ) from e
                elif status_code in (_STATUS_PROCESSING, _STATUS_QUEUED):
                    logger.debug(f"volc_doubao_file: 处理中 status={status_code} msg={status_msg}")
                    time.sleep(_POLL_INTERVAL)
                else:
                    raise TranscriptionError(
                        f"火山 query 失败 status={status_code} msg={status_msg} body={resp.text[:300]}"
                    )
        raise TranscriptionError(f"火山豆包识别超时（>{_POLL_TIMEOUT}s）task_id={task_id}")

    def _parse_result(self, data: Dict[str, Any], task_id: str) -> TranscriptionResponse:
        result = data.get("result") or {}
        full_text = result.get("text", "") or ""

        segments: List[TranscriptionSegment] = []
        for utt in result.get("utterances", []) or []:
            text = (utt.get("text") or "").strip()
            if not text:
                continue
            start_ms = utt.get("start_time")
            end_ms = utt.get("end_time")
            segments.append(TranscriptionSegment(
                text=text,
                start_time=start_ms / 1000.0 if start_ms is not None else None,
                end_time=end_ms / 1000.0 if end_ms is not None else None,
            ))

        duration = None
        audio_info = data.get("audio_info")
        if isinstance(audio_info, dict) and audio_info.get("duration") is not None:
            duration = float(audio_info["duration"]) / 1000.0

        logger.info(
            f"volc_doubao_file: 解析完成 {len(segments)} 句, {len(full_text)} 字"
        )
        return TranscriptionResponse(
            text=full_text,
            segments=segments or None,
            duration=duration,
            language=None,
            provider=self.PROVIDER_NAME,
            model="bigmodel",
            raw_response=data,
        )

    def health_check(self) -> bool:
        return bool(self.app_id and self.access_token)

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["app_id", "access_token"],
            "properties": {
                "app_id": {"type": "string", "description": "火山 APP ID"},
                "access_token": {"type": "string", "description": "火山 Access Token"},
                "resource_id": {
                    "type": "string",
                    "default": "volc.seedasr.auc",
                    "description": "资源 ID（豆包 2.0 = volc.seedasr.auc）",
                },
            },
        }
