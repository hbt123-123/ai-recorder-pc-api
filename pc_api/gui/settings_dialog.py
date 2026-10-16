"""用户配置设置对话框。pc_api / pc_local 共享。

首次启动或用户主动调用时弹出，选择 ASR 四级 tier（凭证硬编码）。
VLM 固定阿里云百炼 Qwen-VL-Max、LLM 统一 DeepSeek，均已硬编码，
无需用户配置。保存到 ~/.airecorder/config.json。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QLabel, QLineEdit, QScrollArea,
    QVBoxLayout, QWidget,
)

from core.transcription.tiers import list_tiers
from core.user_config import UserConfig
from pc_api.gui.styles import Metrics


class SettingsDialog(QDialog):
    """API 配置对话框。"""

    def __init__(self, current: UserConfig = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("配置 API")
        self.resize(680, 720)
        self._cfg = current or UserConfig()
        self._build_ui()
        self._load_current()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(0)
        outer.setContentsMargins(0, 0, 0, 0)

        # ---- 顶部标题区（固定不滚动）----
        header_area = QWidget()
        header_layout = QVBoxLayout(header_area)
        header_layout.setSpacing(Metrics.SPACING_SM)
        header_layout.setContentsMargins(
            Metrics.SPACING_LG, Metrics.SPACING_LG,
            Metrics.SPACING_LG, Metrics.SPACING_MD
        )

        header = QLabel("模型服务配置")
        header.setObjectName("titleLabel")
        header_layout.addWidget(header)

        hint = QLabel(
            "选择转写模型档位（凭证已内置）。"
            "图文描述 VLM 主用智谱 GLM-4.6V-Flash、备用阿里云 Qwen-VL-Max；"
            "文本整理 LLM 主用智谱 GLM-4.7-Flash、备用 DeepSeek，主用失败自动切换，均已硬编码。"
        )
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        header_layout.addWidget(hint)

        outer.addWidget(header_area)

        # ---- 滚动内容区 ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(Metrics.SPACING_MD)
        layout.setContentsMargins(
            Metrics.SPACING_LG, 0,
            Metrics.SPACING_LG, Metrics.SPACING_LG
        )

        # ---- 转写 tier 区 ----
        tier_group = QGroupBox("转写模型（四级选择）")
        tier_form = QFormLayout()
        tier_form.setSpacing(Metrics.SPACING_MD)
        tier_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.combo_tier = QComboBox()
        for t in list_tiers():
            self.combo_tier.addItem(f"{t.label} - {t.model}（{t.price}）", t.tier_id)
        self.combo_tier.currentIndexChanged.connect(self._on_tier_changed)
        tier_form.addRow("档位", self.combo_tier)

        self.label_tier_desc = QLabel()
        self.label_tier_desc.setObjectName("subtitleLabel")
        self.label_tier_desc.setWordWrap(True)
        tier_form.addRow("", self.label_tier_desc)

        self.combo_language = QComboBox()
        self.combo_language.addItem("中文", "zh")
        self.combo_language.addItem("英文", "en")
        self.combo_language.addItem("自动检测", "")
        tier_form.addRow("转写语言", self.combo_language)

        tier_group.setLayout(tier_form)
        layout.addWidget(tier_group)

        # ---- VLM 区（只读）----
        vlm_group = QGroupBox("图文描述 VLM")
        vlm_label = QLabel(
            "主用：智谱 <b>GLM-4.6V-Flash</b>（已内置）<br>"
            "备用：阿里云 <b>Qwen-VL-Max</b>（主用失败自动切换）"
        )
        vlm_label.setObjectName("subtitleLabel")
        vlm_label.setWordWrap(True)
        vlm_layout = QVBoxLayout()
        vlm_layout.addWidget(vlm_label)
        vlm_group.setLayout(vlm_layout)
        layout.addWidget(vlm_group)

        # ---- LLM 区（只读）----
        llm_group = QGroupBox("文本整理 LLM")
        llm_label = QLabel(
            "主用：智谱 <b>GLM-4.7-Flash</b>（已内置，用于融合笔记生成）<br>"
            "备用：DeepSeek <b>deepseek-v4-flash</b>（主用失败自动切换）"
        )
        llm_label.setObjectName("subtitleLabel")
        llm_label.setWordWrap(True)
        llm_layout = QVBoxLayout()
        llm_layout.addWidget(llm_label)
        llm_group.setLayout(llm_layout)
        layout.addWidget(llm_group)

        # ---- VAD 自动分段区 ----
        vad_group = QGroupBox("VAD 自动分段（免费版/专业版）")
        vad_form = QFormLayout()
        vad_form.setSpacing(Metrics.SPACING_MD)
        vad_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.chk_vad_enabled = QCheckBox("说话停顿自动截断分段转写")
        self.chk_vad_enabled.setChecked(True)
        vad_form.addRow("启用", self.chk_vad_enabled)

        self.spin_silence_duration = QDoubleSpinBox()
        self.spin_silence_duration.setRange(0.3, 5.0)
        self.spin_silence_duration.setSingleStep(0.1)
        self.spin_silence_duration.setSuffix(" 秒")
        vad_form.addRow("静音触发时长", self.spin_silence_duration)

        self.combo_silence_threshold = QComboBox()
        self.combo_silence_threshold.addItem("低（安静环境）", 0.01)
        self.combo_silence_threshold.addItem("中（默认）", 0.02)
        self.combo_silence_threshold.addItem("高（嘈杂环境）", 0.04)
        vad_form.addRow("静音灵敏度", self.combo_silence_threshold)

        vad_hint = QLabel(
            "启用后:说话停顿达阈值即截断当前段并独立转写,UI 增量显示,\n"
            "达到接近实时版的体验。专业版 Pro 因 5min 上限,单段达 270s 后强制截断。\n"
            "实时版走 WebSocket 流式,无需此设置。"
        )
        vad_hint.setObjectName("subtitleLabel")
        vad_hint.setWordWrap(True)
        vad_form.addRow("", vad_hint)

        vad_group.setLayout(vad_form)
        layout.addWidget(vad_group)

        # ---- 会议模式区 ----
        meeting_group = QGroupBox("会议模式（视频会议场景）")
        meeting_form = QFormLayout()
        meeting_form.setSpacing(Metrics.SPACING_MD)
        meeting_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.chk_meeting_enabled = QCheckBox("启用会议模式（系统音频 + 截屏 + 悬浮窗）")
        meeting_form.addRow("启用", self.chk_meeting_enabled)

        self.combo_audio_source = QComboBox()
        self.combo_audio_source.addItem("系统声音 + 麦克风（混音，推荐）", "mixed")
        self.combo_audio_source.addItem("仅系统声音（对方发言）", "system")
        self.combo_audio_source.addItem("仅麦克风（本地发言）", "mic")
        meeting_form.addRow("默认音源", self.combo_audio_source)

        self.combo_screenshot_mode = QComboBox()
        self.combo_screenshot_mode.addItem("全屏截屏（默认）", "full")
        self.combo_screenshot_mode.addItem("框选截屏（拖拽选择区域）", "region")
        meeting_form.addRow("截屏方式", self.combo_screenshot_mode)

        self.chk_auto_float = QCheckBox("开始录音时自动收起为悬浮窗")
        meeting_form.addRow("自动收起", self.chk_auto_float)

        self.edit_hotkey = QLineEdit()
        self.edit_hotkey.setPlaceholderText("ctrl+alt+s")
        meeting_form.addRow("截屏快捷键", self.edit_hotkey)

        self.combo_screen_index = QComboBox()
        self._populate_screen_index_combo()
        meeting_form.addRow("截屏显示器", self.combo_screen_index)

        self.spin_meeting_vad_threshold = QDoubleSpinBox()
        self.spin_meeting_vad_threshold.setRange(0.001, 0.1)
        self.spin_meeting_vad_threshold.setSingleStep(0.001)
        self.spin_meeting_vad_threshold.setDecimals(3)
        self.spin_meeting_vad_threshold.setValue(0.01)
        self.spin_meeting_vad_threshold.setToolTip(
            "系统音频电平特性与麦克风不同(静音时可能恒为0、有声音时信号爆表),"
            "复用麦克风阈值会导致乱切/不切。建议 0.01;需用真实会议音频标定。"
        )
        meeting_form.addRow("系统音频 VAD 阈值", self.spin_meeting_vad_threshold)

        meeting_hint = QLabel(
            "会议模式用于视频会议场景：录制系统音频（对方发言）+ 麦克风（本地发言），\n"
            "支持截屏作为附件、主窗口收起为悬浮小窗。会议模式与转写档位正交，\n"
            "可在各 tier 页头部开关。快捷键需以 ctrl/alt/shift 开头。"
        )
        meeting_hint.setObjectName("subtitleLabel")
        meeting_hint.setWordWrap(True)
        meeting_form.addRow("", meeting_hint)

        meeting_group.setLayout(meeting_form)
        layout.addWidget(meeting_group)

        layout.addStretch()

        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # ---- 底部按钮区（固定不滚动）----
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons_layout = QVBoxLayout()
        buttons_layout.setSpacing(0)
        buttons_layout.setContentsMargins(
            Metrics.SPACING_LG, Metrics.SPACING_SM,
            Metrics.SPACING_LG, Metrics.SPACING_LG
        )
        buttons_layout.addWidget(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        btn_container = QWidget()
        btn_container.setLayout(buttons_layout)
        outer.addWidget(btn_container)

    def _populate_screen_index_combo(self) -> None:
        """动态检测显示器并填充下拉框。失败则仅保留主屏。"""
        self.combo_screen_index.addItem("主屏", 0)
        try:
            from PySide6.QtGui import QGuiApplication
            screens = QGuiApplication.screens()
            for i in range(1, len(screens)):
                name = screens[i].name() or f"显示器 {i}"
                self.combo_screen_index.addItem(f"副屏 {i}（{name}）", i)
        except Exception:
            # Qt 不可用时（如测试环境）仅主屏
            pass

    def _load_current(self):
        # tier
        tier_ids = [t.tier_id for t in list_tiers()]
        tidx = tier_ids.index(self._cfg.asr_tier) if self._cfg.asr_tier in tier_ids else 0
        self.combo_tier.setCurrentIndex(tidx)
        self._on_tier_changed()

        # 语言
        lang_map = {"zh": 0, "en": 1, "": 2}
        self.combo_language.setCurrentIndex(lang_map.get(self._cfg.language, 0))

        # VAD 配置
        self.chk_vad_enabled.setChecked(self._cfg.vad_enabled)
        self.spin_silence_duration.setValue(self._cfg.vad_silence_duration)
        threshold_idx = 0
        for i in range(self.combo_silence_threshold.count()):
            if abs(float(self.combo_silence_threshold.itemData(i)) -
                   self._cfg.vad_silence_threshold) < 1e-6:
                threshold_idx = i
                break
        self.combo_silence_threshold.setCurrentIndex(threshold_idx)

        # 会议模式配置
        self.chk_meeting_enabled.setChecked(self._cfg.meeting_mode)
        self._set_combo_by_data(self.combo_audio_source,
                                self._cfg.meeting_audio_source, "mixed")
        self._set_combo_by_data(self.combo_screenshot_mode,
                                self._cfg.meeting_screenshot_mode, "full")
        self.chk_auto_float.setChecked(self._cfg.meeting_auto_float)
        self.edit_hotkey.setText(self._cfg.meeting_hotkey)
        self._set_combo_by_data(self.combo_screen_index,
                                self._cfg.meeting_screen_index, 0)
        self.spin_meeting_vad_threshold.setValue(
            float(self._cfg.meeting_vad_silence_threshold)
        )

    def _on_tier_changed(self):
        tier_id = self.combo_tier.currentData()
        from core.transcription.tiers import get_tier
        t = get_tier(tier_id)
        self.label_tier_desc.setText(
            f"{t.description}\n价格：{t.price}　连接器：{t.connector}"
        )

    def _set_combo_by_data(self, combo, target_data, fallback) -> None:
        """按 itemData 选中 QComboBox,未命中则选 fallback 对应项。"""
        for i in range(combo.count()):
            if combo.itemData(i) == target_data:
                combo.setCurrentIndex(i)
                return
        # 回退到 fallback
        for i in range(combo.count()):
            if combo.itemData(i) == fallback:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def get_config(self) -> UserConfig:
        """返回用户填写的配置。"""
        cfg = self._cfg
        cfg.asr_tier = self.combo_tier.currentData()
        cfg.language = self.combo_language.currentData()

        # VAD 配置
        cfg.vad_enabled = self.chk_vad_enabled.isChecked()
        cfg.vad_silence_duration = float(self.spin_silence_duration.value())
        cfg.vad_silence_threshold = float(self.combo_silence_threshold.currentData())

        # 会议模式配置
        cfg.meeting_mode = self.chk_meeting_enabled.isChecked()
        audio_src = self.combo_audio_source.currentData()
        cfg.meeting_audio_source = audio_src if audio_src in ("mic", "system", "mixed") else "mixed"
        shot_mode = self.combo_screenshot_mode.currentData()
        cfg.meeting_screenshot_mode = shot_mode if shot_mode in ("full", "region") else "full"
        cfg.meeting_auto_float = self.chk_auto_float.isChecked()
        hotkey = self.edit_hotkey.text().strip().lower()
        cfg.meeting_hotkey = hotkey if hotkey else "ctrl+alt+s"
        try:
            cfg.meeting_screen_index = int(self.combo_screen_index.currentData() or 0)
        except (TypeError, ValueError):
            cfg.meeting_screen_index = 0
        cfg.meeting_vad_silence_threshold = float(
            self.spin_meeting_vad_threshold.value()
        )

        # VLM/LLM 均硬编码（主用 GLM + 备用切换，由 config_loader 注入环境变量），
        # 这里仅保留字段向后兼容（config_loader 会覆盖）
        cfg.vlm_model = "glm-4.6v-flash"
        cfg.llm_model = "glm-4.7-flash"
        return cfg
