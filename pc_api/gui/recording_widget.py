"""录音 + 拍照 + 截屏控件。

录音控制（开始/停止）、拍照（OpenCV 摄像头，不可用时降级为选文件）、
截屏（会议模式：QScreen.grabWindow 全屏/框选）。
附件缩略图统一在版本页的「照片附件」Tab 展示，本控件只发出
``photo_added`` 信号触发刷新。
"""

import logging
import os
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFileDialog, QFrame,
)

from pc_api.camera import CameraCapture
from pc_api.screen_capture import ScreenCapture
from pc_api.recording_service import RecordingService
from pc_api.gui.styles import RecordingIndicator, Metrics, get_palette
from pc_api.gui.icons import Icon, make_icon, icon_pixmap
from pc_api.gui.waveform_widget import WaveformWidget

logger = logging.getLogger(__name__)


class RecordingWidget(QFrame):
    """录音控件：状态行 + 录音控制按钮 + 实时声波可视化。"""

    recording_started = Signal(int)   # recording_id
    recording_stopped = Signal(int)   # recording_id
    recording_paused = Signal()       # 录音已暂停
    recording_resumed = Signal()      # 录音已恢复
    photo_added = Signal(int)         # attachment_id
    float_requested = Signal()        # 请求收起为悬浮窗（会议模式）
    status_message = Signal(str)

    def __init__(self, service: RecordingService, parent=None):
        super().__init__(parent)
        self.service = service
        self.camera = CameraCapture()
        self.screen_cap = ScreenCapture()
        self._recording_id = None
        self._audio_path = None
        self._camera_ok = False
        # 分段录音模式开关(True 时 on_start 走 start_segmented_recording,
        # on_stop 跳过 store_audio —— 段在 BaseTierPage/RecordingService 内单独 store)
        self._segmented_mode = False
        # 会议模式开关(True 时显示截屏/悬浮窗按钮,隐藏拍照按钮)
        self._meeting_mode = False
        # 截屏方式:full / region(由 BaseTierPage 从 cfg 同步)
        self._screenshot_mode = "full"
        # 截屏显示器序号
        self._screen_index = 0

        self.setObjectName("Card")
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._build_ui()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_elapsed)
        self._refresh_camera_availability()

    def set_segmented_mode(self, enabled: bool) -> None:
        """切换分段录音模式(VAD 模式下启用)。

        必须在录音未开始时调用。BaseTierPage 根据 tier 在构造时设置一次。
        """
        if self.service.recorder.is_recording:
            logger.warning("set_segmented_mode: 录音中,本次设置将在下次录音生效")
        self._segmented_mode = bool(enabled)

    def set_meeting_mode(self, enabled: bool) -> None:
        """切换会议模式：显示截屏/悬浮窗按钮，隐藏拍照按钮。

        会议模式下「拍照」隐藏（摄像头对会议无意义），「截屏」「悬浮窗」显示。
        非会议模式反之。可在录音中切换（仅影响按钮可见性，不改变当前录音音源）。
        """
        self._meeting_mode = bool(enabled)
        self.btn_capture.setVisible(not enabled)
        self.btn_screenshot.setVisible(enabled)
        self.btn_float.setVisible(enabled)

    def set_screenshot_config(self, mode: str, screen_index: int = 0) -> None:
        """同步截屏方式与显示器序号（由 BaseTierPage 从 cfg 注入）。"""
        self._screenshot_mode = mode if mode in ("full", "region") else "full"
        self._screen_index = int(screen_index)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(Metrics.SPACING_SM)
        layout.setContentsMargins(
            Metrics.SPACING_MD, Metrics.SPACING_MD,
            Metrics.SPACING_MD, Metrics.SPACING_MD
        )

        # ---- 顶部状态行 ----
        header = QHBoxLayout()
        header.setSpacing(Metrics.SPACING_SM)

        self.indicator = RecordingIndicator()
        self.indicator.setVisible(False)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("statusLabel")

        header.addWidget(self.indicator)
        header.addWidget(self.status_label)
        header.addStretch()
        layout.addLayout(header)

        # ---- 控制按钮行 ----
        btn_row = QHBoxLayout()
        btn_row.setSpacing(Metrics.SPACING_SM)

        self.btn_start = QPushButton("  开始录音")
        self.btn_start.setObjectName("primaryButton")
        self.btn_start.setIcon(make_icon("record", 18, get_palette().TEXT_ON_PRIMARY))
        self.btn_pause = QPushButton("  暂停")
        self.btn_pause.setIcon(make_icon("pause", 18))
        self.btn_pause.setEnabled(False)
        self.btn_stop = QPushButton("  停止录音")
        self.btn_stop.setObjectName("dangerButton")
        self.btn_stop.setIcon(make_icon("stop", 18, get_palette().TEXT_ON_PRIMARY))
        self.btn_stop.setEnabled(False)
        self.btn_capture = QPushButton("  拍照")
        self.btn_capture.setIcon(make_icon("camera", 18))
        self.btn_capture.setEnabled(False)
        self.btn_select = QPushButton("  添加图片")
        self.btn_select.setIcon(make_icon("image", 18))
        self.btn_select.setEnabled(False)
        # 会议模式专用：截屏 + 悬浮窗
        self.btn_screenshot = QPushButton("  截屏")
        self.btn_screenshot.setIcon(make_icon("screenshot", 18))
        self.btn_screenshot.setEnabled(False)
        self.btn_screenshot.setVisible(False)  # 默认隐藏，会议模式开启
        self.btn_float = QPushButton("  悬浮窗")
        self.btn_float.setIcon(make_icon("float", 18))
        self.btn_float.setVisible(False)

        self.btn_start.clicked.connect(self.on_start)
        self.btn_pause.clicked.connect(self.on_pause_toggle)
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_capture.clicked.connect(self.on_capture)
        self.btn_select.clicked.connect(self.on_select_file)
        self.btn_screenshot.clicked.connect(self.on_screenshot)
        self.btn_float.clicked.connect(self._on_float_clicked)

        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_pause)
        btn_row.addWidget(self.btn_stop)
        btn_row.addSpacing(Metrics.SPACING_MD)
        btn_row.addWidget(self.btn_capture)
        btn_row.addWidget(self.btn_screenshot)
        btn_row.addWidget(self.btn_select)
        btn_row.addWidget(self.btn_float)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # ---- 实时声波可视化 ----
        self.waveform = WaveformWidget()
        layout.addWidget(self.waveform)

    def _refresh_camera_availability(self):
        self._camera_ok = CameraCapture.available()
        if not self._camera_ok:
            self.btn_capture.setText("  拍照(无摄像头)")
            self.btn_capture.setToolTip("未检测到摄像头，请用「添加图片」")

    def recording_id(self):
        return self._recording_id

    def reset_for_new_recording(self):
        """重置控件状态，准备下一次录音。"""
        self._recording_id = None
        self._audio_path = None
        self.waveform.stop()
        self.waveform.set_level_provider(None)
        self.status_label.setText("就绪")
        self.status_label.setProperty("recording", "false")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.indicator.setVisible(False)
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("  暂停")
        self.btn_pause.setIcon(make_icon("pause", 18))
        self.btn_stop.setEnabled(False)
        self.btn_capture.setEnabled(False)
        self.btn_select.setEnabled(False)
        self.btn_screenshot.setEnabled(False)

    def on_start(self):
        try:
            # 重置上一轮残留状态（按钮态/声波），再创建新录音。
            # 此处调用安全：create_recording/start_recording 随后会重设 id 与按钮态。
            self.reset_for_new_recording()
            # 会议模式音源：从 cfg.meeting_audio_source 读取（由 BaseTierPage 注入）
            # 此处用 _meeting_audio_source 缓存（默认 MIC，非会议模式）
            audio_source = getattr(self, "_meeting_audio_source", None) or "mic"
            is_meeting = self._meeting_mode
            rec = self.service.create_recording(
                audio_source=audio_source if is_meeting else None,
                meeting_mode=is_meeting,
            )
            self._recording_id = rec.id
            if self._segmented_mode:
                # VAD 分段模式:走分段录音,每段独立 wav;段在 BaseTierPage
                # 段截断回调内单独 add_audio_segment 入库,不在此处 store_audio
                if is_meeting:
                    self._audio_path = self.service.start_segmented_recording(
                        rec.id, source=audio_source
                    )
                else:
                    self._audio_path = self.service.start_segmented_recording(rec.id)
            else:
                if is_meeting:
                    self._audio_path = self.service.start_recording(source=audio_source)
                else:
                    self._audio_path = self.service.start_recording()
            self.btn_start.setEnabled(False)
            self.btn_pause.setEnabled(True)
            self.btn_pause.setText("  暂停")
            self.btn_pause.setIcon(make_icon("pause", 18))
            self.btn_stop.setEnabled(True)
            self.btn_capture.setEnabled(self._camera_ok)
            self.btn_select.setEnabled(True)
            self.btn_screenshot.setEnabled(self._meeting_mode)

            self.status_label.setText("录音中… 0.0s")
            self.status_label.setProperty("recording", "true")
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
            self.indicator.setVisible(True)

            self._timer.start(200)
            # 启动声波可视化，注入音量来源（recorder.current_level）
            self.waveform.set_level_provider(self.service.recorder.current_level)
            self.waveform.start()
            self.recording_started.emit(rec.id)
            if self._segmented_mode and is_meeting:
                self.status_message.emit(
                    f"开始录音 (id={rec.id}, 会议模式/{audio_source}, VAD 分段)"
                )
            elif self._segmented_mode:
                self.status_message.emit(f"开始录音 (id={rec.id}, VAD 分段模式)")
            elif is_meeting:
                self.status_message.emit(
                    f"开始录音 (id={rec.id}, 会议模式/{audio_source})"
                )
            else:
                self.status_message.emit(f"开始录音 (id={rec.id})")
        except Exception as e:
            # 会议模式常见的失败是 WASAPI loopback 不可用。录音记录已在
            # 开流前创建，必须标记失败，避免在历史里留下无法处理的 PENDING 条目。
            if self._recording_id is not None:
                try:
                    self.service.set_recording_status(
                        self._recording_id, "FAILED", str(e)
                    )
                except Exception:
                    logger.exception("记录录音启动失败状态时出错")
            self.status_message.emit(f"录音启动失败：{e}")

    def on_stop(self):
        try:
            duration = self.service.stop_recording()
            self._timer.stop()
            # 停止声波可视化
            self.waveform.stop()
            self.waveform.set_level_provider(None)
            # 分段模式:段已在 BaseTierPage 段截断回调内单独 store,此处跳过 store_audio
            # (无单一 _audio_path; 最后段由 BaseTierPage._stop_vad_and_finalize 处理)
            if not self._segmented_mode:
                self.service.store_audio(self._recording_id, self._audio_path)

            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.btn_pause.setText("  暂停")
            self.btn_pause.setIcon(make_icon("pause", 18))
            self.btn_stop.setEnabled(False)
            self.btn_capture.setEnabled(False)
            self.btn_select.setEnabled(False)
            self.btn_screenshot.setEnabled(False)

            self.status_label.setProperty("recording", "false")
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
            self.indicator.setVisible(False)

            if duration is not None:
                self.status_label.setText(f"录音结束 ({duration:.1f}s)")
            else:
                self.status_label.setText("录音结束")
            self.recording_stopped.emit(self._recording_id)
            if self._segmented_mode:
                self.status_message.emit("录音已停止,等待段转写完成…")
            else:
                self.status_message.emit("录音已停止，可开始转写")
        except Exception as e:
            self.status_message.emit(f"停止失败：{e}")

    def on_capture(self):
        if not self._recording_id:
            return
        try:
            staging = self.service.storage.get_staging_dir()
            img_path = os.path.join(staging, f"photo_{int(time.time())}.jpg")
            self.camera.capture(img_path)
            att = self.service.add_attachment(self._recording_id, img_path)
            self.photo_added.emit(att.id)
            self.status_message.emit(f"已拍照 (t={att.timestamp:.1f}s)")
        except Exception as e:
            self.status_message.emit(f"拍照失败：{e}（可改用「添加图片」）")

    def on_select_file(self):
        if not self._recording_id:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.jpg *.jpeg *.png *.bmp)"
        )
        if not path:
            return
        try:
            att = self.service.add_attachment(self._recording_id, path, source="file")
            self.photo_added.emit(att.id)
            self.status_message.emit(f"已添加图片 (t={att.timestamp:.1f}s)")
        except Exception as e:
            self.status_message.emit(f"添加失败：{e}")

    def on_screenshot(self):
        """会议模式截屏：全屏或框选，存为附件。"""
        if not self._recording_id:
            return
        try:
            staging = self.service.storage.get_staging_dir()
            img_path = os.path.join(staging, f"screen_{int(time.time())}.jpg")
            if self._screenshot_mode == "region":
                result = self.screen_cap.capture_region(img_path, parent=self.window())
                if result is None:
                    # 用户取消，静默返回
                    return
            else:
                self.screen_cap.capture_full(img_path, screen_index=self._screen_index)
            att = self.service.add_attachment(
                self._recording_id, img_path, source="screenshot"
            )
            self.photo_added.emit(att.id)
            self.status_message.emit(f"已截屏 (t={att.timestamp:.1f}s)")
        except Exception as e:
            self.status_message.emit(f"截屏失败：{e}")

    def _on_float_clicked(self):
        """悬浮窗按钮：请求主窗口收起为悬浮窗。"""
        self.float_requested.emit()

    def set_meeting_audio_source(self, source: str) -> None:
        """设置会议模式音源（由 BaseTierPage 从 cfg 注入）。"""
        self._meeting_audio_source = source if source in ("mic", "system", "mixed") else "mic"

    def on_pause_toggle(self):
        """暂停/恢复录音切换。"""
        if self.service.recorder.is_paused:
            self.service.resume_recording()
            self.btn_pause.setText("  暂停")
            self.btn_pause.setIcon(make_icon("pause", 18))
            self.status_label.setProperty("recording", "true")
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
            self.indicator.setVisible(True)
            self.waveform.start()
            self.recording_resumed.emit()
            self.status_message.emit("录音已恢复")
        else:
            self.service.pause_recording()
            self.btn_pause.setText("  继续")
            self.btn_pause.setIcon(make_icon("play", 18))
            self.status_label.setProperty("recording", "false")
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
            self.indicator.setVisible(False)
            self.waveform.stop()
            self.recording_paused.emit()
            self.status_message.emit("录音已暂停")

    def _update_elapsed(self):
        elapsed = self.service.recorder.elapsed_seconds()
        if self.service.recorder.is_paused:
            self.status_label.setText(f"已暂停 {elapsed:.1f}s")
        else:
            self.status_label.setText(f"录音中… {elapsed:.1f}s")
