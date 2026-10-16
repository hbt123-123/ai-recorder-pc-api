"""图文融合笔记生成。

三阶段策略（文档 4.5）：
1. 照片实时描述 —— 录音中拍照后调 VLM，已在 vlm_worker 完成，存 attachment.description
2. 图文融合笔记（本模块）—— 录音结束后，转写文本 + 照片描述 + 时间轴 → LLM 生成
   结构化 Markdown 笔记，存入 Recording.summary
3. 语义检索扩展 —— 后续迭代（照片描述嵌入向量，图文混合检索），暂不实现
"""

import logging
from typing import Optional, List

from openai import OpenAI

from core.config import get_config
from core.models import Recording, RecordingAttachment, TranscriptChunk, get_session

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个会议/课堂笔记助手。根据录音转写文本和照片描述，生成结构化的图文融合笔记。

输出要求：
1. 使用 Markdown 格式
2. 结构包含：## 主题摘要、## 关键要点、## 时间轴要点
3. 在相关位置嵌入照片引用，格式：![照片描述](attachment:时间戳秒数)
4. 结合照片描述与转写上下文，突出重要时刻
5. 语言简洁，重点突出，避免照抄转写原文
"""


class NoteFuser:
    """图文融合笔记生成器。

    收集某录音的转写文本（带时间戳 chunks）+ 照片描述 + 时间轴信息，
    调 LLM 生成结构化 Markdown 笔记，存入 Recording.summary。
    """

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None, max_tokens: Optional[int] = None):
        cfg = get_config().llm
        self.api_key = api_key or cfg.api_key
        self.base_url = base_url or cfg.base_url
        self.model = model or cfg.model
        self.max_tokens = max_tokens or cfg.max_tokens

        # 备用 LLM（主用失败时切换）
        self.fallback_api_key = cfg.fallback_api_key
        self.fallback_base_url = cfg.fallback_base_url
        self.fallback_model = cfg.fallback_model

        # 主用/备用任一有 key 即可用云 LLM 融合；均无则用规则模板（pc_local 完全本地）
        self.client = None
        self.fallback_client = None
        if self.api_key:
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        if self.fallback_api_key:
            self.fallback_client = OpenAI(
                api_key=self.fallback_api_key, base_url=self.fallback_base_url
            )

    def _gather_materials(self, recording_id: int) -> dict:
        """收集转写文本、chunks、附件描述。"""
        session = get_session()
        try:
            rec = session.get(Recording, recording_id)
            if not rec:
                raise ValueError(f"Recording {recording_id} not found")

            chunks = (session.query(TranscriptChunk)
                      .filter_by(recording_id=recording_id)
                      .order_by(TranscriptChunk.chunk_index).all())
            attachments = (session.query(RecordingAttachment)
                           .filter_by(recording_id=recording_id)
                           .order_by(RecordingAttachment.timestamp).all())

            for c in chunks:
                session.expunge(c)
            for a in attachments:
                session.expunge(a)

            return {
                "title": rec.title,
                "transcription": rec.transcription or "",
                "chunks": chunks,
                "attachments": attachments,
                "duration": rec.audio_duration_seconds,
            }
        finally:
            session.close()

    def _build_prompt(self, materials: dict) -> str:
        """构造融合笔记的用户 prompt。"""
        parts: List[str] = []
        parts.append(f"# 录音标题：{materials['title']}")
        if materials['duration']:
            parts.append(f"# 录音时长：{materials['duration']:.1f} 秒")

        # 转写文本（优先用带时间戳的 chunks）
        parts.append("\n# 录音转写文本（带时间戳）：")
        chunks: List[TranscriptChunk] = materials['chunks']
        if chunks:
            for c in chunks:
                # start/end 可能独立为空（连接器只保证 start），分别判空防 TypeError
                if c.start_time is not None and c.end_time is not None:
                    ts = f"[{c.start_time:.1f}-{c.end_time:.1f}s] "
                elif c.start_time is not None:
                    ts = f"[{c.start_time:.1f}s] "
                else:
                    ts = ""
                sp = f"[{c.speaker_name}] " if c.speaker_name else ""
                parts.append(f"{ts}{sp}{c.content}")
        else:
            parts.append(materials['transcription'] or "（无转写文本）")

        # 照片描述
        atts: List[RecordingAttachment] = materials['attachments']
        if atts:
            parts.append("\n# 拍摄的照片及 VLM 描述：")
            for a in atts:
                desc = a.description or "（无描述）"
                parts.append(f"- [{a.timestamp:.1f}s] {desc}")
        else:
            parts.append("\n# 拍摄的照片：无")

        return "\n".join(parts)

    def generate_note(self, recording_id: int) -> str:
        """生成融合笔记并保存到 Recording.summary。
        主用/备用任一有 LLM key 则用 LLM 融合；均无则用规则模板拼接。"""
        materials = self._gather_materials(recording_id)

        if self.client or self.fallback_client:
            note = self._generate_with_llm(materials)
            logger.info(f"LLM 融合笔记已保存 recording_id={recording_id}（{len(note)} 字符）")
        else:
            note = self._build_template_note(materials)
            logger.info(f"规则模板笔记已保存 recording_id={recording_id}（{len(note)} 字符）")

        self._save_note(recording_id, note)
        return note

    def _chat(self, system: str, user: str, max_tokens: int,
              temperature: Optional[float] = None) -> str:
        """统一 LLM 调用：主用 GLM，失败立即切换备用 LLM。

        Returns:
            模型输出的文本内容

        Raises:
            Exception: 主备均失败时抛出最后一个异常（调用方决定回退策略）
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            kwargs["temperature"] = temperature
        # 任务型调用（标题/笔记）必须关闭思考链：GLM-4.7 / DeepSeek-v4 系
        # 模型默认先输出 reasoning_content，会把 max_tokens 预算整个吃光
        # （实测 2048 都不够），content 恒为空 → 自动命名/笔记静默失效。
        # 两家均支持 {"thinking": {"type": "disabled"}}；不支持的兼容端点会
        # 忽略未知参数，无副作用。
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        # ---- 主用（未配置主用 key 时抛错落入备用）----
        try:
            if self.client is None:
                raise ValueError("primary LLM not configured")
            response = self.client.chat.completions.create(**kwargs)
            if response.choices:
                content = response.choices[0].message.content or ""
                if content.strip():
                    return content
            raise ValueError("primary LLM returned empty content")
        except Exception as e:
            logger.warning(f"主用 LLM({self.model}) 失败，切换备用: {e}")
            if not self.fallback_client:
                raise

        # ---- 备用 ----
        fb_kwargs = dict(kwargs)
        fb_kwargs["model"] = self.fallback_model
        logger.info(f"使用备用 LLM: {self.fallback_model}")
        response = self.fallback_client.chat.completions.create(**fb_kwargs)
        # 与主用路径对称：空响应视为失败抛错，避免空笔记静默覆盖 Recording.summary
        if response.choices:
            content = response.choices[0].message.content or ""
            if content.strip():
                return content
        raise ValueError("fallback LLM returned empty content")

    def _generate_with_llm(self, materials: dict) -> str:
        """用云 LLM 生成融合笔记。主备均失败时回退规则模板。"""
        prompt = self._build_prompt(materials)
        try:
            logger.info(f"生成融合笔记 model={self.model}")
            content = self._chat(
                system=SYSTEM_PROMPT,
                user=prompt,
                max_tokens=self.max_tokens,
            )
            return content
        except Exception as e:
            logger.error(f"LLM 融合笔记生成失败，回退规则模板: {e}")
            return self._build_template_note(materials)

    def _build_template_note(self, materials: dict) -> str:
        """规则模板拼接 Markdown 笔记（无 LLM，完全本地）。

        结构：# 标题 / ## 主题摘要 / ## 关键要点 / ## 时间轴要点。
        时间轴把转写分段与照片按时间戳合并排列。
        """
        parts: List[str] = []
        parts.append(f"# {materials['title'] or '录音笔记'}")
        if materials['duration']:
            parts.append(f"\n> 录音时长：{materials['duration']:.1f} 秒\n")

        chunks: List[TranscriptChunk] = materials['chunks']
        transcription: str = materials['transcription'] or ""
        full_text = "".join(c.content for c in chunks) if chunks else transcription

        # 主题摘要：转写前 200 字
        parts.append("## 主题摘要\n")
        summary = full_text[:200].strip()
        parts.append(summary + ("..." if len(full_text) > 200 else ""))

        # 关键要点：转写分段
        parts.append("\n## 关键要点\n")
        if chunks:
            for c in chunks:
                ts = f"[{c.start_time:.0f}s] " if c.start_time is not None else ""
                parts.append(f"- {ts}{c.content}")
        elif transcription:
            for line in transcription.split('\n'):
                if line.strip():
                    parts.append(f"- {line.strip()}")

        # 时间轴要点：照片 + 转写按时间合并
        parts.append("\n## 时间轴要点\n")
        atts: List[RecordingAttachment] = materials['attachments']
        timeline = []
        for c in chunks:
            timeline.append((c.start_time or 0.0, 'text', c.content))
        for a in atts:
            timeline.append((a.timestamp or 0.0, 'photo', a.description or ''))
        timeline.sort(key=lambda x: x[0])
        for ts, kind, content in timeline:
            if kind == 'photo':
                parts.append(f"- [{ts:.0f}s] [照片] {content}")
            else:
                parts.append(f"- [{ts:.0f}s] {content}")

        return "\n".join(parts)

    def _save_note(self, recording_id: int, note: str) -> None:
        session = get_session()
        try:
            rec = session.get(Recording, recording_id)
            if rec:
                rec.summary = note
                session.commit()
        finally:
            session.close()

    # ---------- 会话标题自动生成 ----------

    TITLE_SYSTEM_PROMPT = """你是会话标题生成器。根据录音转写文本,用中文输出一个 ≤12 字的简洁标题。
要求:
- 抓住核心主题/讨论内容
- 不用句号、引号
- 不用"关于""主题是"等冗余前缀
- 只输出标题文本本身,不要解释
"""

    def generate_title(self, transcript: str) -> str:
        """用 LLM 生成 ≤12 字会话标题。失败 / 无任何 LLM key / 文本过短时返回空串(调用方走 fallback)。

        复用 _chat(OpenAI 兼容,主备切换),与 _generate_with_llm 同源,无新依赖。
        输入截断到 1500 字符避免超 token;max_tokens=64 限制输出长度;
        temperature=0.3 保证稳定性。

        清理规则:用正则从两侧去掉空白 + 引号 + 中英文标点。
        注意:先 rstrip 句末标点,再 strip 两侧引号(顺序敏感,句号在引号外时
        不会卡住 strip 的边界扫描)。
        """
        if not self.client and not self.fallback_client:
            return ""
        if not transcript or len(transcript.strip()) < 20:
            return ""
        try:
            content = self._chat(
                system=self.TITLE_SYSTEM_PROMPT,
                user=transcript[:1500],
                max_tokens=64,
                temperature=0.3,
            )
            title = content.strip()
            if not title:
                return ""
            # 清理:句末标点 + 两侧引号/空白(用正则更鲁棒,避免 strip 卡边界)
            import re
            # 1. 去中间换行 / Tab 等空白
            title = re.sub(r"\s+", "", title)
            # 2. 去两侧中英文句末标点
            title = re.sub(r"^[。.!?,;：:、]+", "", title)
            title = re.sub(r"[。.!?,;：:、]+$", "", title)
            # 3. 去两侧引号(中英文 / 全角)
            title = re.sub(r'^["\'"\'「」『』《》]+', "", title)
            title = re.sub(r'["\'"\'「」『』《》]+$', "", title)
            return title[:12]
        except Exception as e:
            logger.warning(f"LLM 生成标题失败: {e}")
            return ""
