"""PC API 版本入口。

启动时检查用户配置（~/.airecorder/config.json），无则弹 OnboardingDialog 引导
选择 ASR tier。VLM 主用智谱 GLM-4.6V-Flash（备用百炼 Qwen-VL-Max）、LLM 主用
智谱 GLM-4.7-Flash（备用 DeepSeek），凭证硬编码无需用户配置。
运行：python main.py（在 ai-recorder-pc-api/ 目录下执行）
"""

import ctypes
import logging
import os
import sys
from pathlib import Path

# main.py 位于版本根，core 与 pc_api 均为同级包
_MAIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_MAIN_DIR))
# ./data/ 等相对路径依赖工作目录，统一切到版本根，允许从任意目录启动
os.chdir(_MAIN_DIR)

# Windows 任务栏：设置 AppUserModelID，让任务栏显示自己的图标而非 Python 图标。
# 必须在 QApplication 创建之前调用。
if sys.platform == "win32":
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ai.recorder.app")

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from core.models import init_db
from core.user_config import UserConfig, load_user_config, save_user_config
from pc_api.gui.onboarding_dialog import OnboardingDialog
from pc_api.gui.settings_dialog import SettingsDialog
from pc_api.recording_service import RecordingService
from pc_api.gui.main_window import MainWindow
from pc_api.gui.styles import apply_global_styles


def ensure_config() -> UserConfig:
    """加载用户配置；无则弹首启引导页，未完成引导/配置缺失则弹设置对话框。

    首启流程：
      1. 无配置文件 → 轻量引导页（欢迎 → 选转写档位 → 完成），替代直接弹设置。
      2. 有配置但未完成过引导（旧版本升级）→ 补一次引导。
      3. 配置损坏（asr_tier 为空）→ 回退设置对话框。
    用户取消引导/设置则退出。

    VLM 与 LLM 均硬编码，启动检查仅需转写 tier。
    """
    cfg = load_user_config()
    if cfg is None:
        # 首次启动：弹轻量引导页
        dialog = OnboardingDialog()
        if dialog.exec() != OnboardingDialog.DialogCode.Accepted:
            sys.exit(0)
        cfg = UserConfig()
        cfg.asr_tier = dialog.get_selected_tier()
        cfg.onboarding_done = True
        save_user_config(cfg)
        return cfg

    if not cfg.onboarding_done:
        # 旧版本升级：补一次引导，选择转写档位
        dialog = OnboardingDialog()
        if dialog.exec() == OnboardingDialog.DialogCode.Accepted:
            cfg.asr_tier = dialog.get_selected_tier()
        cfg.onboarding_done = True
        save_user_config(cfg)
        return cfg

    if not cfg.is_transcription_configured():
        # 配置缺失/损坏：回退设置对话框
        dialog = SettingsDialog(cfg)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            sys.exit(0)
        cfg = dialog.get_config()
        save_user_config(cfg)
    return cfg


def main() -> int:
    # 日志同时落文件：启动失败时（尤其 Windows pythonw 无控制台）可查 data/startup_error.log
    _MAIN_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = QApplication(sys.argv)
    app.setApplicationName("AI 录音")

    icon_path = Path(__file__).resolve().parent / "assets" / "app.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # 首次启动弹配置对话框
    cfg = ensure_config()
    apply_global_styles(app, theme=cfg.theme)

    # 注入环境变量
    from pc_api.config_loader import apply_config
    apply_config(cfg)

    init_db()

    service = RecordingService()
    window = MainWindow(service, cfg)
    window.show()
    return app.exec()


def _run_with_crash_dialog() -> int:
    """顶层异常兜底：写日志 + 弹窗告知，避免双击/pythonw 启动时窗口闪退无诊断。"""
    try:
        return main()
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001  入口兜底需覆盖一切
        import traceback
        tb = traceback.format_exc()
        try:
            logging.getLogger(__name__).error("启动失败:\n%s", tb)
            log_file = _MAIN_DIR / "data" / "startup_error.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(tb, encoding="utf-8")
        except Exception:
            pass
        # QApplication 已存在则复用；未创建阶段失败也尽力弹出可见提示
        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv)
        try:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(
                None, "AI 录音 启动失败",
                f"{type(e).__name__}: {e}\n\n详细堆栈已写入:\n{_MAIN_DIR / 'data' / 'startup_error.log'}",
            )
        except Exception:
            print(tb, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_run_with_crash_dialog())
