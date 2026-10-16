"""VLM 图片描述后台任务（QThread）。

对录音下所有 PENDING 附件生成描述，结合时间轴附近转写文本作为上下文。

prompt 按附件 source 细分（PROMPT_BY_SOURCE）：
- screenshot：侧重图表/PPT 文字/白板信息
- camera/file：描述场景；meeting_mode 仅用于日志
"""

import logging

from PySide6.QtCore import QThread, Signal

from core.models import get_session
from core.vlm import get_vlm_connector, VLMRequest
from core.vlm.base import VLMError, VLMConfigurationError
from core.fusion.timeline import get_context_text_for_attachment
from pc_api.recording_service import RecordingService

logger = logging.getLogger(__name__)

# Prompt 按场景区分（会议模式 vs 普通模式）
PROMPT_MEETING = (
    "请描述这张屏幕截图的内容，侧重图表/PPT 文字/白板信息，识别关键文字。"
    "如有会议共享屏幕、文档、表格，请提取主要文字内容。"
)
PROMPT_NORMAL = (
    "请描述这张图片的内容，并尽量结合录音上下文理解其场景。"
)
# 附件来源细分的 prompt（覆盖会议/普通二分）
PROMPT_BY_SOURCE = {
    "screenshot": PROMPT_MEETING,
    "camera": PROMPT_NORMAL,
    "file": PROMPT_NORMAL,
}


class VLMWorker(QThread):
    """VLM 描述工作线程：批量处理某录音下所有 PENDING 附件。"""

    progress = Signal(int, str)               # (attachment_id, 消息)
    finished_ok = Signal(int, str)            # (attachment_id, 描述)
    failed = Signal(int, str)                 # (attachment_id, 错误)
    all_done = Signal(int)                    # recording_id（全部处理完）

    def __init__(self, service: RecordingService, recording_id: int):
        super().__init__()
        self.service = service
        self.recording_id = recording_id

    def _resolve_prompt(self, attachment_source: str) -> str:
        """根据附件来源决定 prompt。

        按附件 source（screenshot/camera/file）查表；
        未知 source 回退 PROMPT_NORMAL。
        """
        return PROMPT_BY_SOURCE.get(attachment_source or "camera", PROMPT_NORMAL)

    def run(self) -> None:
        rid = self.recording_id
        try:
            self._run_impl(rid)
        except Exception as e:
            # 兜底：任何未预期异常都必须有终态信号，否则 GUI 永久挂起
            logger.exception(f"VLMWorker 顶层异常 rec={rid}")
            self.failed.emit(-1, f"VLM 任务异常终止：{e}")
        finally:
            # 唯一出口统一发射 all_done（早退路径也覆盖），且只发一次
            self.all_done.emit(rid)

    def _run_impl(self, rid: int) -> None:
        attachments = self.service.list_attachments(rid)
        pending = [a for a in attachments if a.description_status == "PENDING"]

        if not pending:
            return

        # 查询录音整体是否会议模式（仅用于日志；prompt 实际按附件 source 细分）
        is_meeting = False
        rec = self.service.get_recording(rid)
        if rec is not None:
            is_meeting = bool(getattr(rec, "meeting_mode", False))
        logger.info(
            f"VLMWorker 启动: rec={rid} meeting_mode={is_meeting} "
            f"pending={len(pending)}"
        )

        try:
            connector = get_vlm_connector()
        except VLMConfigurationError as e:
            msg = f"VLM 配置错误：{e}（请检查 DASHSCOPE_API_KEY）"
            for att in pending:
                self.failed.emit(att.id, msg)
                # 同步落库，避免重启后附件仍停留在 PENDING 被反复重试
                try:
                    self.service.set_attachment_status(att.id, "FAILED")
                except Exception:
                    logger.exception(f"附件状态落库失败 att={att.id}")
            return

        for att in pending:
            try:
                self.progress.emit(att.id, "生成描述…")
                image_path = self.service.resolve_local_path(att.storage_locator)

                # 取时间轴附近转写文本作为上下文
                session = get_session()
                try:
                    context = get_context_text_for_attachment(session, att, window_seconds=30.0)
                finally:
                    session.close()

                prompt = self._resolve_prompt(getattr(att, "source", None) or "camera")

                request = VLMRequest(
                    image_path=image_path,
                    prompt=prompt,
                    context_text=context or None,
                )
                response = connector.describe_image(request)
                self.service.save_attachment_description(att.id, response.description)
                self.finished_ok.emit(att.id, response.description)

            except VLMError as e:
                logger.error(f"VLM 描述失败 att={att.id}: {e}")
                # 状态落库失败不应阻断后续附件与终态信号
                try:
                    self.service.set_attachment_status(att.id, "FAILED")
                except Exception:
                    logger.exception(f"附件状态落库失败 att={att.id}")
                self.failed.emit(att.id, f"描述失败：{e}")
            except Exception as e:
                logger.exception(f"VLM 异常 att={att.id}")
                try:
                    self.service.set_attachment_status(att.id, "FAILED")
                except Exception:
                    logger.exception(f"附件状态落库失败 att={att.id}")
                self.failed.emit(att.id, f"未知错误：{e}")

