# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（AI 录音 PC API 版）。

构建命令：
    pyinstaller build.spec

产物：dist/AIRecorder/ 目录 + dist/AIRecorder.exe 入口。

体积背景：
- 旧版对 PySide6 无脑 collect_all('PySide6')，把整套 Qt 技术栈搬进产物：
  QtWebEngine/Chromium（约 150MB）、3D / Charts / DataVisualization / Quick3D /
  Pdf 等 Addons、全部官方翻译 .qm（约 30MB）、整个 PySide6/qml 目录（约 20MB），
  外加每一个 Qt6*.dll 与 PySide6 *.pyd，最终包体约 700MB。
- 本应用实际只 import 四个模块：QtCore / QtGui / QtWidgets / QtMultimedia
  （音频播放）。上述资源全部用不上，现已改为「excludes 声明式排除 +
  Analysis 之后的 TOC 兜底过滤」双保险，功能零损失。
  实测（Linux，同一份 spec）：1029MB → 463MB，其中 PySide6 目录 673MB → 144MB。
- 体积下限说明：PySide6（Qt + FFmpeg 后端）+ numpy 已占去绝大部分预算，
  因此「< 100MB」在不放弃 opencv 的前提下无法达成；成品里 cv2 仍约 80MB
  （仅用于摄像头抓帧）。若要进一步压缩，需先把摄像头采集改为 Qt Multimedia。
- Qt 插件（platforms / imageformats / styles / multimedia 等）无需手动收集：
  PyInstaller 内置的 PySide6 hooks 会按实际 import 自动带上所需插件。

验证方法（打包后逐项手测）：
- 启动 dist/AIRecorder/AIRecorder.exe，确认界面正常、控制台无报错；
- 历史页音频播放（QtMultimedia）、摄像头拍照、截屏选区均正常；
- 录音 + 转写（含 realtime 档位）正常。
- 若启动异常：先把 USE_UPX 改为 False 重新打包，再按被剔除清单逐项排查。
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# UPX 压缩开关：UPX 压缩 Qt 官方 DLL 有可能触发杀软误报，个别环境下还会
# 出现解压加载失败导致 exe 无法启动；产物启动异常时改为 False 重打包即可。
USE_UPX = True

# 不交给 UPX 压缩的运行时关键 DLL：压缩 python3xx.dll / MSVC 运行库是已知的
# 启动失败与杀软误报来源，收益又远不及 Qt 那些大体积 DLL。Qt6 系列仍参与压缩。
_UPX_EXCLUDE = [
    'vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll',
    'python3*.dll', 'ucrtbase.dll',
]

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
# 注意：PySide6 不再 collect_all（700MB 包体的元凶，见文件头说明），
# Qt 资源改由 PyInstaller 内置 PySide6 hooks 按实际 import 自动收集。
# 每个包单独 try/except：pyaudiowpatch 等 Windows-only 包在其它平台或
# 未安装时不应让 spec 解析失败。
for pkg in ('pyaudiowpatch', 'pynput', 'soxr'):
    try:
        d, b, h = collect_all(pkg)
    except Exception as exc:  # ImportError 及 hook 内部异常均视为「本机不可用」
        print(f"[build.spec] 警告：collect_all('{pkg}') 失败，已跳过该包：{exc}")
        continue
    datas += d
    binaries += b
    hiddenimports += h

# 显式补齐 PyInstaller 静态分析看不到的动态导入（多为函数内延迟导入或
# __import__("xxx") 字符串导入，例如 core/audio/sources.py 的 pyaudiowpatch）。
hiddenimports += [
    'numpy',                # 音频数组 / VAD，部分文件为延迟导入
    'sounddevice',          # PortAudio 录音，函数内延迟导入
    'soundfile',            # WAV 读写
    'sqlalchemy',           # ORM，engine 运行时反射加载
    'openai',               # VLM / LLM 连接器
    'httpx',                # ASR 连接器（dashscope / volc 直连 HTTP）
    'dashscope',            # 阿里云百炼 SDK
    'dashscope.audio.asr',  # 实时转写子模块，运行时按字符串导入
    'paramiko',             # pro_lite SFTP 上传，函数内延迟导入
    'pyaudiowpatch',        # __import__("pyaudiowpatch") 字符串导入
    'cv2',                  # 摄像头拍照，函数内延迟导入
    'soxr',                 # 重采样扩展，函数内延迟导入
    'pynput',               # 全局热键
    'dotenv',               # pc_api/credentials.py 读取 .env
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
    excludes=[
        # ---- 未使用的重型第三方库（避免被间接依赖拖入）----
        'torch', 'torchvision', 'tensorflow', 'scipy', 'pandas', 'matplotlib',
        'PIL', 'Pillow', 'PIL.Image', 'pyarrow', 'sklearn', 'librosa', 'av',
        'yaml', 'tkinter', 'IPython', 'jupyter', 'notebook', 'pytest',
        'tests', 'generate_icons', 'test_ai_chains',
        # ---- PySide6 未使用的 Addons + 部分 Essentials ----
        # 保留：QtCore / QtGui / QtWidgets / QtMultimedia / QtNetwork
        #（QtMultimedia 可能依赖 QtNetwork）以及 shiboken6、PySide6/plugins，
        # 切勿加入本列表。
        'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebEngineQuick', 'PySide6.QtWebChannel',
        'PySide6.QtWebSockets', 'PySide6.QtCharts',
        'PySide6.QtDataVisualization', 'PySide6.QtGraphs',
        'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DInput',
        'PySide6.Qt3DLogic', 'PySide6.Qt3DAnimation', 'PySide6.Qt3DExtras',
        'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtQuick3D',
        'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtPositioning',
        'PySide6.QtLocation', 'PySide6.QtSensors', 'PySide6.QtSerialPort',
        'PySide6.QtSerialBus', 'PySide6.QtRemoteObjects', 'PySide6.QtScxml',
        'PySide6.QtStateMachine', 'PySide6.QtTextToSpeech',
        'PySide6.QtSpatialAudio', 'PySide6.QtNetworkAuth',
        'PySide6.QtHttpServer', 'PySide6.QtVirtualKeyboard',
        'PySide6.QtDesigner', 'PySide6.QtHelp', 'PySide6.QtUiTools',
        'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtQml', 'PySide6.QtQuick',
        'PySide6.QtQuickWidgets', 'PySide6.QtQuickControls2',
        'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets',
        'PySide6.QtMultimediaWidgets', 'PySide6.QtSvgWidgets',
        'PySide6.QtSvg', 'PySide6.QtPrintSupport', 'PySide6.QtDBus',
        'PySide6.QtConcurrent', 'PySide6.QtXml',
        'PySide6.QtNetworkInformation',
    ],
    cipher=block_cipher,
    noarchive=False,
)


# ---- 防御性 TOC 过滤 ------------------------------------------------------
# excludes 之外的兜底：即使某些 Qt6*.dll / qml / translations 因 hook 行为
# 变化混进 TOC，也在这里按目标路径剔除，绝不进入 COLLECT。
# 只统计、不逐文件打印（动辄上千条）。
# 实测约束（切勿「顺手」扩大下方剔除清单）：
# - Qt6Qml* / Qt6Quick* 必须保留：Qt Multimedia 的 FFmpeg 后端插件
#   plugins/multimedia/*ffmpegmediaplugin* 依赖它们；而本应用上传白名单含
#   .flac/.ogg/.mp3/.m4a，播放器必须走 FFmpeg 后端，不能改用 Windows 原生
#   后端（原生后端只覆盖 wav/mp3，会丢 m4a/aac/flac 播放能力）。
# - Qt6Svg / Qt6Pdf / Qt6VirtualKeyboard 保留：分别被已随包的插件
#   iconengines/qsvgicon、imageformats/qpdf、platforminputcontexts/
#   qtvirtualkeyboardplugin 依赖，剔除会留下加载失败的悬空插件。
#   三者合计仅数 MB，不值得冒风险。
# - avcodec-* / avformat-* / avutil-* / swresample-* / swscale-* 保留（同上）。

# 目标路径 basename 命中以下前缀的 Qt 库一律剔除；只列「无插件依赖」的
# Addons / 未用模块，故不会造成悬空插件。Windows 侧形如 Qt6Sql.dll，
# Linux 侧为 libQt6Sql.so.6，_category() 会剥离 lib 前缀后统一比对，
# 因此同一套规则在 Linux 上也能被真实检验。
_DLL_PREFIXES = (
    'Qt6WebEngine', 'Qt6Quick3D', 'Qt63D', 'Qt6Charts',
    'Qt6DataVisualization', 'Qt6Graphs', 'Qt6Designer', 'Qt6Help',
    'Qt6Sql', 'Qt6Test', 'Qt6Sensors', 'Qt6Bluetooth', 'Qt6Nfc',
    'Qt6Positioning', 'Qt6Location', 'Qt6SerialPort', 'Qt6SerialBus',
    'Qt6RemoteObjects', 'Qt6Scxml', 'Qt6TextToSpeech',
    'Qt6SpatialAudio', 'Qt6NetworkAuth', 'Qt6HttpServer',
    'Qt6SvgWidgets',
)
_DLL_PREFIXES_LOWER = tuple(p.lower() for p in _DLL_PREFIXES)

# 每类剔除条目的计数器，最后各打印一行中文汇总
_dropped = {'qml': 0, 'translations': 0, 'qt-dll': 0}


def _dest_str(entry):
    """取 TOC 条目的目标路径，归一化为 posix 风格字符串（兼容 str / pathlib.Path）。"""
    try:
        dest = entry[0]
    except (TypeError, IndexError):
        return ''
    if hasattr(dest, 'as_posix'):  # pathlib.Path / PurePath
        return dest.as_posix()
    return str(dest).replace('\\', '/')


def _category(dest):
    """命中剔除规则返回类别（'qml' / 'translations' / 'qt-dll'），否则 None。"""
    low = dest.lower()
    if 'pyside6/qml/' in low:
        return 'qml'
    if 'pyside6/translations/' in low:
        return 'translations'
    base = low.rsplit('/', 1)[-1]
    is_lib = base.endswith('.dll') or '.so.' in base or base.endswith('.so')
    if not is_lib:
        return None
    # Windows 侧为 Qt6Sql.dll；Linux 侧为 libQt6Sql.so.6——必须剥离 lib 前缀，
    # 否则 .so 分支永远匹配不上（曾因此让 Linux 侧的规则形同虚设）。
    stem = base[3:] if base.startswith('lib') else base
    if stem.startswith(_DLL_PREFIXES_LOWER):
        return 'qt-dll'
    return None


def _keep(dest, dropped=None):
    """目标路径是否保留；传入 dropped 计数器时按类别累计被剔除条目数。"""
    category = _category(dest)
    if category is None:
        return True
    if dropped is not None:
        dropped[category] += 1
    return False


def _filter_toc(toc):
    """对 TOC 逐条过滤（datas / binaries / zipfiles 同为 (dest, src, type) 三元组）。"""
    return [entry for entry in toc if _keep(_dest_str(entry), _dropped)]


a.datas = _filter_toc(a.datas)
a.binaries = _filter_toc(a.binaries)
a.zipfiles = _filter_toc(a.zipfiles)

print(f"[build.spec] 已剔除 PySide6/qml 条目 {_dropped['qml']} 个")
print(f"[build.spec] 已剔除 PySide6/translations 条目 {_dropped['translations']} 个")
print(f"[build.spec] 已剔除无用 Qt6*.dll 条目 {_dropped['qt-dll']} 个")

# ---------------------------------------------------------------------------

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
    upx=USE_UPX,
    console=True,        # 保留控制台便于查看日志；发布版可改 False
    icon='assets/app.ico',  # 任务栏/资源管理器图标（与运行时窗口图标同源）
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=USE_UPX,
    upx_exclude=_UPX_EXCLUDE,
    name='AIRecorder',
)
