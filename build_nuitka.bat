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

setlocal
if not exist "myenv\Scripts\activate.bat" (
    echo [ERROR] 请先创建并激活 myenv 虚拟环境
    exit /b 1
)
call myenv\Scripts\activate.bat

rem ============================================================
rem 产物体积说明：
rem 1) Nuitka standalone 目录不做任何内置压缩（PyInstaller 常见做法是
rem    UPX 压缩后再打包），所以原始体积天然偏大；实测同等内容的
rem    PyInstaller 未压缩包为 1029MB，与本产物 ~995MB 基本持平——
rem    体积来自应用依赖本身，而不是 Nuitka 这个打包器。
rem 2) 下方已用 --nofollow-import-to 排除应用从不 import 的重依赖，
rem    并关闭 Qt 翻译文件与 setuptools / pytest / unittest / pydoc /
rem    IPython 等无关子模块的收录。
rem 3) 修改后请实测体积：若仍 >200MB，剩余大头主要是 cv2（OpenCV，
rem    约 80MB）和 numpy 等运行时必需依赖——这与 PyInstaller 打包
rem    面临的是同一组取舍，可参考 build.spec 中已做的裁剪。
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
endlocal
