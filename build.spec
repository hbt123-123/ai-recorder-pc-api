# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（AI 录音 PC API 版）。

构建命令：
    pyinstaller build.spec

产物：dist/AIRecorder/ 目录 + dist/AIRecorder.exe 入口。

关键点：
- 含 DLL/so 的依赖（pyaudiowpatch / pynput / soxr）必须 collect_all 确保二进制
  资源打包，否则运行时 ImportError 或 DLL 加载失败。
- PySide6 用 collect_all 收齐 plugins/translations/QML 资源。
- numpy / sounddevice 显式列入 hiddenimports 防止动态导入被树摇掉。
- 排除 matplotlib / tkinter / pytest 减小体积（本项目未使用）。
- 控制台窗口保留（--console），便于查看日志；如需无控制台版本改 console=False。
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# ---- 收集含原生二进制（DLL/so）的依赖 ----
datas = []
binaries = []
hiddenimports = []

# 应用图标等静态资源：main.py / main_window / floating_window 均按
# 包根相对路径查找 assets/，必须原样落入 bundle 根（onedir 下即 _internal/）
datas += [('assets', 'assets')]

# credentials.py（gitignored，PyInstaller 不会自动包含）：
# 运行时 from pc_api import credentials 必需，否则启动崩溃
datas += [('pc_api/credentials.py', 'pc_api')]

# pyaudiowpatch: PyAudio fork，含 PortAudio DLL + WASAPI loopback 支持
# pynput: 全局热键，Win 下含 DLL（mouse/keyboard hook）
# soxr: libsamplerate 绑定，含 native 扩展
# PySide6: Qt plugins / translations / QML 资源
for pkg in ('pyaudiowpatch', 'pynput', 'soxr', 'PySide6'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# 显式补齐动态导入的纯 Python 模块（部分通过 importlib 运行时加载）
hiddenimports += [
    'numpy',
    'sounddevice',
    'soundfile',
    'sqlalchemy',
    'openai',
    'httpx',
    'dashscope',
    'paramiko',
]

# 项目内子模块（确保 workers / gui 子包完整打包）
hiddenimports += collect_submodules('core')
hiddenimports += collect_submodules('pc_api')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['matplotlib', 'tkinter', 'pytest', 'IPython', 'jupyter'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AIRecorder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,        # 保留控制台便于查看日志；发布版可改 False
    icon='assets/app.ico',  # 任务栏/资源管理器图标（与运行时窗口图标同源）
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AIRecorder',
)
