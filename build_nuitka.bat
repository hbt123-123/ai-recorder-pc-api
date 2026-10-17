@echo off
rem ============================================================
rem AI 录音 PC API 版 — Nuitka 打包脚本（在 Windows 开发机运行）
rem
rem 前置条件：
rem   1. myenv 虚拟环境已激活，requirements.txt 已安装
rem   2. pip install nuitka  （首次运行会自动下载 MinGW64 编译器，
rem      或已装 VS/MSVC 则直接使用）
rem   3. 首次全量编译约 20~60 分钟，需 10GB+ 磁盘与 8GB+ 内存
rem
rem 产物：dist\AIRecorder\AIRecorder.exe（standalone 目录模式）
rem
rem 注意：Nuitka 不支持交叉编译——必须在 Windows 上构建 Windows 产物。
rem       不要用 --onefile：本应用启动时 chdir 到程序目录并在其下创建
rem       data\（数据库/录音），onefile 每次解压到随机临时目录会导致
rem       数据写进临时区、重启即丢。
rem ============================================================
rem 产物体积与 UPX 事后压缩说明：
rem   Nuitka 不做内置压缩，standalone 产物里的 DLL/PYD 默认未压缩，体积会
rem   明显偏大；可用 UPX 对产物内的 DLL/PYD 做事后压缩，通常能显著缩减体积。
rem   主程序 AIRecorder.exe 有意不压缩——Nuitka/PyInstaller 的 exe 经 UPX
rem   压缩后可能无法启动。
rem   UPX 可能触发个别杀软误报；如遇启动异常，删除 tools\upx_compress_dist.ps1
rem   的压缩结果即可（重新打包会生成新的未压缩产物）。
rem   启用方式：构建前设置环境变量 UPX_COMPRESS=1（详见文件末尾逻辑）。
rem ============================================================

setlocal
if not exist "myenv\Scripts\activate.bat" (
    echo [ERROR] 请先创建并激活 myenv 虚拟环境
    exit /b 1
)
call myenv\Scripts\activate.bat

rem ============================================================
rem 产物体积说明：
rem 1) Nuitka standalone 目录不做任何内置压缩（PyInstaller 常见做法是
rem    UPX 压缩后再打包），所以原始体积天然偏大——体积来自应用依赖
rem    本身，而不是 Nuitka 这个打包器。
rem 2) 本机实测（2026-10-17，Windows x64，Python 3.12）：
rem      PyInstaller onedir 未压缩 305.6MB → 让 UPX 真正生效后 127.8MB（-58%）。
rem      测试机上的 PyInstaller 297.8MB 与 Nuitka 389.3MB 都是「未压缩」状态，
rem      所以最大的一刀不是换打包器，而是让 UPX 真正跑起来（见末尾 UPX 块）。
rem 3) 下方已用 --nofollow-import-to 排除应用从不 import 的重依赖，并关闭
rem    Qt 翻译文件与 setuptools / pytest / unittest / pydoc / IPython 等
rem    无关子模块的收录；--noinclude-dlls 再做文件级裁剪（见下方注释）。
rem 4) 修改后请实测体积（python tools\report_dist_size.ps1 -Path dist\main.dist）；
rem    若仍偏大，剩余大头是 cv2（约 112MB）、numpy/OpenBLAS（约 26MB）与
rem    QtMultimedia 的 FFmpeg（约 18MB，播放 m4a/flac/ogg 所需）等运行时
rem    必需依赖——取舍记录见 build.spec 头部「体积背景」。
rem ============================================================

rem ============================================================
rem 幽灵依赖裁剪（--noinclude-dlls，与 build.spec / tools\trim_dist.py 同源）：
rem   以下 DLL 经「PE 导入表反查」确认无人引用（证据与推导见 build.spec 头部）：
rem     - opengl32sw.dll：软件 OpenGL 兜底，纯 QWidget 应用不创建 GL 上下文；
rem     - Qt6Qml*/Qt6Quick*/Qt6OpenGL*：唯一拖入它们的根源是虚拟键盘插件，
rem       应用实际走 QtMultimedia 的 FFmpeg 播放插件（导入表只依赖
rem       Qt6Multimedia.dll + Qt6Core.dll）；
rem     - Qt6Pdf / Qt6Svg / Qt6VirtualKeyboard：应用不读写 PDF/SVG、不用虚拟键盘；
rem     - opencv_videoio_ffmpeg*.dll：cv2 仅用于相机抓帧（Windows 走 MSMF/DSHOW），
rem       该 DLL 只服务 CAP_FFMPEG 读视频文件；
rem     - libportaudio32bit*/arm64/dylib：x64-Windows 产物用不到的非本机架构变体。
rem   若某次构建因这些参数报错、或运行期发现功能异常，删掉对应行重建即可；
rem   也可以保持参数不动、改用 TRIM=1 在构建后按文件裁剪（效果相同）。
rem ============================================================
python -m nuitka ^
  --standalone ^
  --enable-plugin=pyside6 ^
  --noinclude-qt-translations ^
  --python-flag=no_site ^
  --include-data-dir=assets=assets ^
  --include-package=core ^
  --include-package=pc_api ^
  --include-package=pyaudiowpatch ^
  --include-package-data=pyaudiowpatch ^
  --include-package-data=sounddevice ^
  --include-package-data=soxr ^
  --include-package=cv2 ^
  --include-package=pynput ^
  --include-package=dashscope ^
  --include-package=paramiko ^
  --noinclude-setuptools-mode=nofollow ^
  --noinclude-pytest-mode=nofollow ^
  --noinclude-unittest-mode=nofollow ^
  --noinclude-pydoc-mode=nofollow ^
  --noinclude-IPython-mode=nofollow ^
  --nofollow-import-to=matplotlib ^
  --nofollow-import-to=tkinter ^
  --nofollow-import-to=pytest ^
  --nofollow-import-to=tests ^
  --nofollow-import-to=scipy ^
  --nofollow-import-to=pandas ^
  --nofollow-import-to=torch ^
  --nofollow-import-to=torchvision ^
  --nofollow-import-to=tensorflow ^
  --nofollow-import-to=IPython ^
  --nofollow-import-to=jupyter ^
  --nofollow-import-to=notebook ^
  --nofollow-import-to=PIL ^
  --nofollow-import-to=pyarrow ^
  --nofollow-import-to=sklearn ^
  --nofollow-import-to=librosa ^
  --nofollow-import-to=yaml ^
  --python-flag=no_docstrings ^
  --noinclude-dlls=Qt6Qml*.dll ^
  --noinclude-dlls=Qt6Quick*.dll ^
  --noinclude-dlls=Qt6OpenGL*.dll ^
  --noinclude-dlls=Qt6Pdf.dll ^
  --noinclude-dlls=Qt6Svg.dll ^
  --noinclude-dlls=Qt6VirtualKeyboard.dll ^
  --noinclude-dlls=opengl32sw.dll ^
  --noinclude-dlls=opencv_videoio_ffmpeg*.dll ^
  --noinclude-dlls=libportaudio32bit*.dll ^
  --noinclude-dlls=libportaudioarm64*.dll ^
  --noinclude-dlls=libportaudio.dylib ^
  --windows-console-mode=force ^
  --output-dir=dist ^
  --output-filename=AIRecorder.exe ^
  --windows-icon-from-ico=assets\app.ico ^
  --company-name="AI Recorder" ^
  --product-name="AI Recorder PC API" ^
  --file-version=1.0.0.0 ^
  --show-progress ^
  main.py

if errorlevel 1 (
    echo [ERROR] Nuitka 构建失败
    exit /b 1
)
echo.
echo [OK] 构建完成: dist\main.dist\AIRecorder.exe
echo 如需目录名固定为 AIRecorder，可将 dist\main.dist 重命名或改用
rem   --output-dir=dist 与手工重命名（Nuitka 的 standalone 目录名固定为 main.dist）

rem ============================================================
rem 可选：幽灵文件裁剪 TRIM（仅在环境变量 TRIM=1 时执行）
rem   先用 PE 导入表反查确认的「无人引用」清单裁剪产物，再交给 UPX 压缩——
rem   顺序为先 TRIM 后 UPX，避免给马上要删的文件白白压缩。
rem   裁剪清单与依据见 tools\trim_dist.py（默认 dry-run，此处用 --apply）。
rem   图省事也可在裁剪后按需重命名 dist\main.dist 为 AIRecorder。
rem ============================================================
if "%TRIM%"=="1" (
    echo [INFO] TRIM=1，开始裁剪产物中的幽灵文件 ...
    python tools\trim_dist.py dist\main.dist --apply
    if errorlevel 1 (
        echo [WARN] 裁剪脚本返回非零，请查看上方输出（产物本身仍可用）
    )
) else (
    echo [INFO] 未设置 TRIM=1，跳过幽灵文件裁剪
)

rem ============================================================
rem 可选：UPX 事后压缩（仅在环境变量 UPX_COMPRESS=1 时执行）
rem ============================================================
if "%UPX_COMPRESS%"=="1" (
    echo [INFO] UPX_COMPRESS=1，开始压缩产物 DLL/PYD ...
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\upx_compress_dist.ps1 -Path dist\main.dist
    if errorlevel 1 (
        echo [WARN] UPX 压缩脚本返回非零，请查看上方输出（产物本身仍可用）
    )
) else (
    echo [INFO] 未设置 UPX_COMPRESS=1，跳过产物压缩（体积会明显偏大）
)
endlocal
