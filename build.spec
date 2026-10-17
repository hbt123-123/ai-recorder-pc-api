# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller 打包配置（AI 录音 PC API 版）。

构建命令：
    pyinstaller build.spec

产物：dist/AIRecorder/ 目录 + dist/AIRecorder.exe 入口。

体积背景（两轮瘦身）：
- 第一轮：不再 collect_all('PySide6')，改为 excludes 声明式排除 + TOC 兜底过滤，
  剔掉 QtWebEngine/Chromium、3D / Charts / DataVisualization / Quick3D /
  Pdf 等 Addons、全部官方翻译 .qm、整个 PySide6/qml 目录，功能零损失。
- 第二轮（本版）：按「对产物全量 PE 导入表反查」的结果继续剔除幽灵依赖
  （每个候选项先扫 dist 内全部 PE 文件，确认除彼此外没有任何引用者才剔除）：
  * opengl32sw.dll（约 19.7MB）：软件 OpenGL 兜底库，只在真正创建 GL 上下
    文时才动态加载；本应用是纯 QWidget/QPainter 界面（无 QOpenGLWidget、
    无 Qt Quick），无任何 PE 文件引用它。
  * Qt6Qml / Qt6Quick / Qt6OpenGL / Qt6QmlModels / Qt6QmlMeta /
    Qt6QmlWorkerScript / Qt6VirtualKeyboard（合计约 14.4MB）：唯一把它们
    拖进产物的是虚拟键盘插件 platforminputcontexts/qtvirtualkeyboardplugin.dll
    （桌面物理键盘应用不需要）。注意：多媒体 FFmpeg 播放插件
    (plugins/multimedia/ffmpegmediaplugin.dll) 的导入表只依赖
    Qt6Multimedia.dll + Qt6Core.dll，与 Qml/Quick 无关——旧注释「必须保留
    Qt6Qml/Qt6Quick」是误判，反查已证伪。
  * Qt6Pdf.dll（4.4MB）+ imageformats/qpdf.dll、Qt6Svg.dll（0.6MB）+
    iconengines/qsvgicon.dll、imageformats/qsvg.dll：应用不读写 PDF/SVG
    （assets 全为 png/ico，界面图标由 QPainter 绘制）。
  * 其余未用插件：qdirect2d / qoffscreen / qminimal（备用 QPA，不设置
    QT_QPA_PLATFORM 时永不加载）；qtiff / qwebp / qgif / qicns / qtga /
    qwbmp（拍照与截屏只产 .jpg，选图对话框限定 jpg/jpeg/png/bmp，png/bmp
    为 QtGui 内置格式；保留 qjpeg 供 jpg、qico 供应用图标）。
  * cv2 的 FFmpeg videoio 后端 opencv_videoio_ffmpeg*_64.dll（约 29.5MB）：
    应用只做相机抓帧，Windows 上 CAP_ANY 先走 MSMF/DSHOW，该 DLL 仅服务
    CAP_FFMPEG 读视频文件。可用下方 DROP_CV2_FFMPEG 开关恢复。
- UPX：PyInstaller 只在 PATH 里能执行到 upx.exe 时才压缩，找不到会「静默
  跳过」——产物直接大 100MB+ 且没有任何报错（此前 297.8MB 的产物即未压缩
  状态）。本 spec 会自动探测 tools/upx-*/upx.exe 并加入 PATH，探测失败时
  打印醒目中文警告。UPX 为预编译二进制，不需要任何 C++ 工具链；本仓库已把
  UPX 5.2.1 win64 预编译包提交在 tools/upx-5.2.1-win64/（约 1.7MB），构建机
  开箱即用（换版本/缺失时跑 tools/fetch_upx.ps1 重新下载即可）。
- 体积下限说明：PySide6（Qt + FFmpeg 后端）+ numpy(OpenBLAS) + cv2 已占去
  绝大部分预算；若要再压掉 cv2 的约 112MB，需把摄像头采集改为
  Qt Multimedia（QCamera/QImageCapture，见 pc_api/camera.py），属代码改造。

验证方法（打包后逐项手测）：
- 启动 dist/AIRecorder/AIRecorder.exe，确认界面正常、控制台无报错；
- 历史页音频播放（QtMultimedia FFmpeg 后端；务必覆盖 .m4a/.flac/.ogg 各
  一首）、摄像头拍照、截屏选区均正常；
- 录音 + 转写（含 realtime 档位）正常。
- 若启动异常：先把 USE_UPX 改为 False 重新打包（排除 UPX 因素），再按被
  剔除清单逐项排查。
- 若音频播放异常：优先怀疑虚拟键盘/Qml 剔除链条，把 _PLUGIN_DROP 中
  platforminputcontexts 一条与 _DLL_PREFIXES 中 'Qt6Qml' / 'Qt6Quick' /
  'Qt6OpenGL' / 'Qt6VirtualKeyboard' 注释掉重建即可恢复。

实测记录（2026-10-17，Windows x64 / PyInstaller 6.22.2 / UPX 5.2.1 / Python 3.12.6）：
- 同机基线：未压缩 305.6MB（347 文件）→ 仅让 UPX 生效 127.8MB → 叠加本
  轮裁剪 107.8MB（318 文件，较未压缩基线 -64.7%）；主 exe 17.1MB。
  测试机此前的 PyInstaller 297.8MB / Nuitka 389.3MB 均为「UPX 未生效」的
  未压缩产物——最大的一刀不是换打包器，而是让 UPX 真正跑起来。
- 剔除计数（构建日志）：translations 124、Qt6*.dll 15（含 PortAudio 非
  x64 变体 5）、Qt 插件 13、cv2 FFmpeg videoio 1。
- 体积下限：产物中 68 个带 CFG 的 PE 文件共 50.9MB（Qt6Core/Gui/Widgets、
  bootloader exe 与多数 Qt 插件），PyInstaller 与 UPX 都按安全策略拒绝压缩
  （对 Qt DLL 用 upx --force 实测可压到 ~34%，但 CFG 守护表失效有运行
  风险）。如需再压这一档，用 tools\upx_compress_dist.ps1 -Force 并做完整
  回归（启动、播放 m4a/flac/ogg、摄像头、截屏、录音转写）后再决定。
- 剩余大头：cv2.pyd 20.3MB（UPX 后）、主 exe 17.1MB、Qt6Core/Qt6Gui/
  Qt6Widgets 25.2MB（CFG 不可压）、python312.dll 6.6MB、FFmpeg 全家桶
  （av*) 7.4MB、numpy/OpenBLAS 3.7MB。
- 首启目录：产物首次运行时 data/ 目录由 core.models.init_engine 自动创建
  （此前缺失会导致 "unable to open database file" 启动失败——属既有打包
  问题，已修）；相机照片保存改走 imencode + write_bytes，中文路径安全
  （既有 cv2.imwrite 在非 ASCII 路径下会失败/乱码，已修）。
- 凭证 .env 查找顺序（pc_api/credentials.py）：冻结态优先 exe 同目录 .env
  （分发时放 exe 旁即可），其次打包资源目录（PyInstaller onedir 为
  _internal\.env，历史位置）；开发态保持仓库根目录。多个文件按序互补缺失项，
  且不覆盖已有环境变量。
"""

import glob
import os
import shutil

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# ---- UPX 自动探测 -----------------------------------------------------------
# 探测顺序：tools/upx-*/upx.exe（如 tools/upx-5.2.1-win64/upx.exe，取版本号
# 最大者）→ tools/upx/upx.exe → 系统 PATH。找到即注入 PATH 供 PyInstaller
# 的 upx 压缩步骤使用；找不到时大声警告（否则会静默产出未压缩的巨大包）。
_SPEC_DIR = globals().get('SPECPATH', os.getcwd())
_upx_candidates = []
for _pat in ('tools/upx*/upx.exe', 'tools/upx/*/upx.exe', 'tools/upx/upx.exe'):
    _upx_candidates += sorted(glob.glob(os.path.join(_SPEC_DIR, _pat)))
for _cand in reversed(_upx_candidates):
    if os.path.isfile(_cand):
        os.environ['PATH'] = os.path.dirname(_cand) + os.pathsep + os.environ.get('PATH', '')
        break
if shutil.which('upx'):
    print(f"[build.spec] 已启用 UPX 压缩：{shutil.which('upx')}")
else:
    print('[build.spec] 警告：未找到 upx.exe，产物将不做任何压缩（预计大出 100MB+）！'
          '请把 UPX win64 发行包解压到 tools/ 下（如 tools/upx-5.2.1-win64/upx.exe）'
          '或将其加入 PATH 后重新打包。')

# UPX 压缩开关：UPX 压缩 Qt 官方 DLL 有可能触发杀软误报，个别环境下还会
# 出现解压加载失败导致 exe 无法启动；产物启动异常时改为 False 重打包即可。
USE_UPX = True

# cv2 自带 FFmpeg videoio 后端（opencv_videoio_ffmpeg*_64.dll，约 29.5MB）：
# 应用只用 cv2.VideoCapture(相机索引) + cv2.imwrite，Windows 默认后端为
# MSMF/DSHOW，不会加载该 DLL（它只服务 CAP_FFMPEG 读视频文件/网络流）。
# 若将来需要 cv2 读视频文件，把此开关改为 False 重新打包即可。
DROP_CV2_FFMPEG = True

# 不交给 UPX 压缩的运行时关键 DLL：压缩 python3xx.dll / MSVC 运行库是已知的
# 启动失败与杀软误报来源，收益又远不及 Qt 那些大体积 DLL。Qt6 系列仍参与压缩。
# 与 PyInstaller 内置默认排除清单保持一致的超集。
_UPX_EXCLUDE = [
    'vcruntime140.dll', 'vcruntime140_1.dll', 'vcruntime140_2.dll',
    'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
    'concrt140.dll', 'vccorlib140.dll', 'vcamp140.dll', 'vcomp140.dll',
    'python3*.dll', 'ucrtbase.dll', 'api-ms-win-*.dll',
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
        #（QtMultimedia 依赖 QtNetwork）以及 shiboken6、PySide6/plugins，
        # 切勿加入本列表。
        # 注意：Python 层 excludes 只能挡住「被 import 的模块」；下面这些
        # Qt6*.dll 会经由多媒体/虚拟键盘等插件的二进制依赖闭包混进 TOC，
        # 所以必须由 Analysis 之后的 TOC 兜底过滤（见 _PLUGIN_DROP /
        # _DLL_PREFIXES）再拦一道。
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
# excludes 之外的兜底：即使某些 Qt6*.dll / 插件 / qml / translations 因 hook
# 行为变化混进 TOC，也在这里按目标路径剔除，绝不进入 COLLECT。
# 只统计、不逐文件打印（动辄上千条）。
#
# 剔除依据：对一次完整产物做「PE 导入表反查」——扫描 dist 内全部文件（693
# 个）中对每个候选 DLL 名的 ASCII 引用，确认候选之间的依赖是闭环的、且没有
# 其它文件引用它们之后才剔除。未曾验证的条目不要凭印象加进来。
# 必须保留（已实测依赖）：
# - plugins/multimedia/ffmpegmediaplugin.dll：本应用上传白名单含
#   .flac/.ogg/.mp3/.m4a，播放器必须走 FFmpeg 后端（Windows 原生后端只覆盖
#   wav/mp3，会丢 m4a/aac/flac）。它只依赖 Qt6Multimedia.dll + Qt6Core.dll。
# - plugins/multimedia/windowsmediaplugin.dll：备用原生后端，体积仅 0.3MB。
# - 保留的图片插件 qjpeg（拍照/截屏 .jpg）与 qico（应用图标）。
# - plugins/styles/qmodernwindowsstyle.dll（Windows 11 风格）、
#   platforms/qwindows.dll（标准 QPA）、networkinformation/qnetworklistmanager
#   （QtNetwork 依赖）、generic/qtuiotouchplugin（触摸输入）。

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
    # ---- 第二轮新增（反查确认闭环、无外部引用）----
    'Qt6Qml', 'Qt6Quick', 'Qt6OpenGL', 'Qt6VirtualKeyboard',
    'Qt6Pdf', 'Qt6Svg',
    # 软件 OpenGL 兜底（纯 QWidget 应用不创建 GL 上下文）
    'opengl32sw',
    # sounddevice 携带的多架构 PortAudio：x64-Windows 产物只需要 64bit 两个
    # 变体（libportaudio64bit.dll 与 libportaudio64bit-asio.dll），32bit /
    # arm64 / macOS 变体在同一产物里永远加载不到。
    'portaudio32bit', 'portaudioarm64', 'portaudio.dylib',
)
_DLL_PREFIXES_LOWER = tuple(p.lower() for p in _DLL_PREFIXES)

# 按完整目标路径（posix 风格、小写、后缀匹配）精确剔除的插件文件。
# 与上面的 DLL 前缀清单配合：插件与被它拉进来的 DLL 必须成对剔除，
# 否则会留下「悬空插件」（运行时加载失败刷日志）。
_PLUGIN_DROP = (
    # 虚拟键盘：桌面物理键盘应用不需要；且它是唯一把 Qt6VirtualKeyboard /
    # Qt6Quick / Qt6Qml / Qt6OpenGL 拖进产物的根源。
    'pyside6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll',
    # SVG 图标引擎：应用不用 SVG（assets 全为 png/ico）。
    'pyside6/plugins/iconengines/qsvgicon.dll',
    # 备用 QPA 平台插件：不设置 QT_QPA_PLATFORM 环境变量时永不加载。
    'pyside6/plugins/platforms/qdirect2d.dll',
    'pyside6/plugins/platforms/qoffscreen.dll',
    'pyside6/plugins/platforms/qminimal.dll',
    # 图片格式插件：只保留 qjpeg（.jpg）与 qico（.ico）；png/bmp 是 QtGui
    # 内置格式，其余格式应用既不产出也读不到（选图对话框限定
    # jpg/jpeg/png/bmp）。
    'pyside6/plugins/imageformats/qpdf.dll',
    'pyside6/plugins/imageformats/qsvg.dll',
    'pyside6/plugins/imageformats/qtiff.dll',
    'pyside6/plugins/imageformats/qwebp.dll',
    'pyside6/plugins/imageformats/qgif.dll',
    'pyside6/plugins/imageformats/qicns.dll',
    'pyside6/plugins/imageformats/qtga.dll',
    'pyside6/plugins/imageformats/qwbmp.dll',
)

# 每类剔除条目的计数器，最后各打印一行中文汇总
_dropped = {'qml': 0, 'translations': 0, 'qt-dll': 0, 'qt-plugin': 0, 'cv2-ffmpeg': 0}


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
    """命中剔除规则返回类别（'qml' / 'translations' / 'qt-dll' / 'qt-plugin'
    / 'cv2-ffmpeg'），否则 None。"""
    low = dest.lower()
    if 'pyside6/qml/' in low:
        return 'qml'
    if 'pyside6/translations/' in low:
        return 'translations'
    if any(low.endswith(p) for p in _PLUGIN_DROP):
        return 'qt-plugin'
    base = low.rsplit('/', 1)[-1]
    is_lib = base.endswith(('.dll', '.dylib')) or '.so.' in base or base.endswith('.so')
    if not is_lib:
        return None
    if DROP_CV2_FFMPEG and base.startswith('opencv_videoio_ffmpeg'):
        return 'cv2-ffmpeg'
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
print(f"[build.spec] 已剔除无用 Qt 插件条目 {_dropped['qt-plugin']} 个")
print(f"[build.spec] 已剔除 cv2 FFmpeg videoio 条目 {_dropped['cv2-ffmpeg']} 个")

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
