"""全局热键管理（视频会议模式截屏快捷键）。

基于 pynput 实现系统级全局热键，应用失焦时也能触发。
pynput 不可用（无 GUI / 无 DISPLAY / 某些锁屏环境）时回退到应用内 QShortcut。

设计要点：
- pynput.keyboard.GlobalHotKeys 在独立线程监听，callback 在该线程触发,
  必须通过 Qt 信号 QueuedConnection 切回主线程执行（不能在监听线程操作 GUI）。
- 注册失败（热键冲突 / 解析失败）返回 False，调用方回退 QShortcut。
- stop() 必须调用，否则监听线程泄漏。
"""

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# pynput 可选依赖：测试环境 / 无 GUI 环境可能未安装
try:
    from pynput import keyboard as _pk
    _PYNPUT_AVAILABLE = True
except Exception:  # ImportError / RuntimeError（某些平台无键盘 hook）
    _pk = None
    _PYNPUT_AVAILABLE = False


class GlobalHotkeyManager:
    """全局热键管理器：注册系统级快捷键。

    用法:
        mgr = GlobalHotkeyManager()
        ok = mgr.register("ctrl+alt+s", lambda: print("截屏"))
        if not ok:
            # 回退 QShortcut
            ...
        # 退出时
        mgr.stop()
    """

    def __init__(self):
        self._listener = None  # pynput GlobalHotKeys listener
        self._registered: dict[str, Callable] = {}  # hotkey_str -> callback

    @staticmethod
    def available() -> bool:
        """pynput 是否可用（环境支持全局热键）。"""
        return _PYNPUT_AVAILABLE

    def register(self, hotkey_str: str, callback: Callable) -> bool:
        """注册一个全局热键。

        Args:
            hotkey_str: 热键字符串，如 "ctrl+alt+s"（pynput 格式，不区分大小写）
            callback: 触发回调（无参数）

        Returns:
            True 注册成功；False 注册失败（pynput 不可用 / 热键格式错误 / 已有 listener）
        """
        if not _PYNPUT_AVAILABLE:
            logger.info("pynput 不可用，全局热键注册失败")
            return False

        hotkey_str = (hotkey_str or "").strip().lower()
        if not hotkey_str:
            logger.warning("热键字符串为空")
            return False

        # 已有 listener 时先停掉（简化模型：单 listener，重新注册全部）
        self._stop_listener()

        self._registered[hotkey_str] = callback
        if self._rebuild_listener():
            logger.info(f"全局热键已注册: {hotkey_str}")
            return True
        # 重建失败（常见为新热键字符串非法）：回滚本次注册并基于剩余热键
        # 再尝试重建，避免原有可用热键随单次失败一起静默失效
        self._registered.pop(hotkey_str, None)
        self._rebuild_listener()
        return False

    def unregister(self, hotkey_str: str) -> None:
        """注销一个热键（重新构造 listener）。"""
        hotkey_str = (hotkey_str or "").strip().lower()
        if hotkey_str not in self._registered:
            return
        self._registered.pop(hotkey_str, None)
        # 重新注册剩余热键；失败时保留注册表，下次 register 可恢复
        self._stop_listener()
        if self._registered and _PYNPUT_AVAILABLE:
            self._rebuild_listener()

    def _rebuild_listener(self) -> bool:
        """按当前 _registered 全量重建 listener。成功 True，失败置空返回 False。"""
        if not _PYNPUT_AVAILABLE or not self._registered:
            return False
        try:
            pynput_hotkeys = {
                _to_pynput_format(k): v for k, v in self._registered.items()
            }
            self._listener = _pk.GlobalHotKeys(pynput_hotkeys)
            self._listener.start()
            return True
        except Exception as e:
            logger.warning(f"全局热键监听重建失败: {e}")
            self._listener = None
            return False

    def _stop_listener(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception as e:
                # 停止失败意味着钩子线程可能仍存活，必须留痕供排查
                logger.warning(f"停止热键监听线程失败: {e}")
            self._listener = None

    def stop(self) -> None:
        """停止所有热键监听（应用退出时调用）。"""
        self._stop_listener()
        self._registered.clear()


def _to_pynput_format(hotkey_str: str) -> str:
    """将内部格式 ctrl+alt+s 转为 pynput 格式 <ctrl>+<alt>+s。

    pynput GlobalHotKeys 要求修饰键用尖括号包裹（<ctrl> / <alt> / <shift> / <cmd>），
    普通键直接写字符（s / f1）。传入不带尖括号的 ctrl 会导致解析失败。
    """
    modifiers = {"ctrl", "alt", "shift", "cmd"}
    parts = []
    for p in hotkey_str.split("+"):
        p = p.strip()
        if p in modifiers:
            parts.append(f"<{p}>")
        else:
            parts.append(p)
    return "+".join(parts)


def normalize_hotkey(hotkey_str: str) -> str:
    """规范化热键字符串：小写 + 去多余空格。

    例如 "Ctrl + Alt + S" -> "ctrl+alt+s"
    """
    parts = [p.strip().lower() for p in (hotkey_str or "").split("+") if p.strip()]
    return "+".join(parts)


def validate_hotkey(hotkey_str: str) -> bool:
    """简单校验热键字符串格式：至少含一个修饰键 + 一个普通键。

    修饰键: ctrl / alt / shift / cmd
    """
    s = normalize_hotkey(hotkey_str)
    if not s:
        return False
    parts = s.split("+")
    if len(parts) < 2:
        return False
    modifiers = {"ctrl", "alt", "shift", "cmd"}
    has_modifier = any(p in modifiers for p in parts)
    has_normal = any(p not in modifiers for p in parts)
    return has_modifier and has_normal
