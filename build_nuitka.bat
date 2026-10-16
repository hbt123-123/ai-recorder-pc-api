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

python -m nuitka ^
  --standalone ^
  --enable-plugin=pyside6 ^
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
  --nofollow-import-to=matplotlib ^
  --nofollow-import-to=tkinter ^
  --nofollow-import-to=pytest ^
  --nofollow-import-to=tests ^
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
