#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包产物「幽灵文件」裁剪脚本（stdlib only，Python 3.12）。

用途：对「已经构建好的产物目录」（PyInstaller 的 dist\\AIRecorder 或
Nuitka 的 dist\\main.dist）做文件级裁剪——移除经 PE 导入表反查确认
「无人引用」的幽灵文件。默认干跑（dry-run），必须显式 --apply 才会删除。

用法：
    python tools\\trim_dist.py dist\\AIRecorder            # 干跑：只预览命中项
    python tools\\trim_dist.py dist\\AIRecorder --list     # 只打印命中清单
    python tools\\trim_dist.py dist\\AIRecorder --apply    # 实际删除命中项

安全约束：
  * 只删除下方规则命中的「文件」，绝不递归删除目录；
  * 任何位于 assets\\ 目录下的文件一律跳过；
  * 文件名匹配忽略大小写；
  * 目标目录必须存在且看起来像产物目录（含顶层 .exe 或 _internal 目录）。

删除后请按文件末尾的中文回归清单实测；异常时重新构建产物即可恢复。
"""

import argparse
import fnmatch
import os
import sys

# 控制台与重定向输出统一 UTF-8，避免中文乱码
# （与 tools\report_dist_size.ps1 的 Console.OutputEncoding=UTF8 保持一致）。
# Windows 控制台底层为 UTF-8 字节流，故此处对控制台与管道/文件均安全。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

MB = 1024.0 * 1024.0

# ---------------------------------------------------------------------------
# 幽灵文件规则：(匹配模式, 中文裁剪依据)
# 模式为「小写 basename」，支持 fnmatch 通配符 '*'：
#   前缀匹配写成 'xxx*.dll'，精确匹配直接写完整文件名（如 'opengl32sw.dll'）。
# 逐条依据见下方注释。
# ---------------------------------------------------------------------------
_RULES = [
    # —— 软件 OpenGL 兜底实现 ——
    # 纯 QWidget/QPainter 应用不创建 GL 上下文，不需要软件 OpenGL 回退。
    ("opengl32sw.dll",
     "软件 OpenGL 兜底，纯 QWidget/QPainter 应用不创建 GL 上下文"),

    # —— Qt6 QML / Quick / OpenGL / Pdf / Svg / 虚拟键盘 ——
    # 唯一拖入它们的根是虚拟键盘插件；多媒体 FFmpeg 插件只依赖
    # Qt6Multimedia.dll + Qt6Core.dll，不依赖 QML/Quick/OpenGL。
    ("qt6qml*.dll",
     "Qt6 QML 运行时；唯一拖入根是虚拟键盘插件，应用未使用 QML"),
    ("qt6quick*.dll",
     "Qt6 Quick 运行时；随 QML 一并被虚拟键盘插件拖入，应用未使用"),
    ("qt6opengl*.dll",
     "Qt6 OpenGL 运行时；随 QML/Quick 被拖入，应用不创建 GL 上下文"),
    ("qt6pdf.dll",
     "Qt6 Pdf 模块；仅被已删除的 qpdf.dll 插件引用，应用未使用"),
    ("qt6svg.dll",
     "Qt6 Svg 模块；仅被已删除的 qsvg/qsvgicon 插件引用，应用未使用"),
    ("qt6virtualkeyboard.dll",
     "Qt6 虚拟键盘模块；应用无屏幕键盘需求，未使用"),

    # —— 未使用的 Qt 插件 ——
    ("qtvirtualkeyboardplugin.dll",
     "虚拟键盘平台输入上下文插件；应用无屏幕键盘需求"),
    ("qsvgicon.dll",
     "SVG 图标引擎插件；图标均为内置 ico/png，不需要 SVG"),
    ("qpdf.dll",
     "PDF 图片格式插件；应用不读写 PDF 图像"),
    ("qsvg.dll",
     "SVG 图片格式插件；应用不读写 SVG 图像"),
    ("qtiff.dll",
     "TIFF 图片格式插件；应用不读写 TIFF 图像"),
    ("qwebp.dll",
     "WebP 图片格式插件；应用不读写 WebP 图像"),
    ("qgif.dll",
     "GIF 图片格式插件；应用不读写 GIF 图像"),
    ("qicns.dll",
     "ICNS 图片格式插件；应用不读写 ICNS 图像"),
    ("qtga.dll",
     "TGA 图片格式插件；应用不读写 TGA 图像"),
    ("qwbmp.dll",
     "WBMP 图片格式插件；应用不读写 WBMP 图像"),
    ("qdirect2d.dll",
     "Direct2D 平台插件；Windows 默认走 qwindows，未使用"),
    ("qoffscreen.dll",
     "离屏平台插件；应用无离屏渲染需求，未使用"),
    ("qminimal.dll",
     "最小平台插件；仅用于无 GUI 场景，应用未使用"),

    # 有意保留的 Qt 插件（不在删除清单内）：
    #   qjpeg / qico / qwindows / qmodernwindowsstyle /
    #   qtuiotouchplugin / qnetworklistmanager /
    #   ffmpegmediaplugin / windowsmediaplugin

    # —— cv2 的 FFmpeg videoio 后端 ——
    # 应用只用相机抓帧 + imwrite；Windows 默认走 MSMF/DSHOW，
    # 写图使用内置编码器，无需该 FFmpeg 后端。
    ("opencv_videoio_ffmpeg*.dll",
     "OpenCV FFmpeg videoio 后端；相机走 MSMF/DSHOW、写图走内置编码器"),

    # —— 非 x64-Windows 架构的 PortAudio 变体 ——
    # 必须保留 libportaudio64bit.dll 与 libportaudio64bit-asio.dll 两个。
    ("libportaudio32bit*.dll",
     "32 位 PortAudio 变体；产物为 x64，运行时只会加载 64bit 版"),
    ("libportaudioarm64*.dll",
     "ARM64 PortAudio 变体；产物为 x64，运行时不会加载"),
    ("libportaudio.dylib",
     "macOS PortAudio 变体；Windows 产物不会加载 dylib"),
]


def _match_rule(name_lower):
    """返回命中的 (模式, 依据)；未命中返回 None。name_lower 必须已小写。"""
    for pattern, reason in _RULES:
        if fnmatch.fnmatchcase(name_lower, pattern):
            return pattern, reason
    return None


def _in_assets(rel_path):
    """判断相对路径是否位于 assets 目录下（只看目录部分，忽略大小写）。"""
    parts = rel_path.replace("\\", "/").split("/")
    return any(p.lower() == "assets" for p in parts[:-1])


def _looks_like_dist(root):
    """产物目录校验：至少含顶层 .exe 文件或 _internal 目录。"""
    try:
        entries = os.listdir(root)
    except OSError:
        return False
    for name in entries:
        full = os.path.join(root, name)
        if os.path.isdir(full) and name.lower() == "_internal":
            return True
        if os.path.isfile(full) and name.lower().endswith(".exe"):
            return True
    return False


def _collect_hits(root):
    """遍历产物目录，返回命中列表：[(相对路径, 字节数, 模式, 依据), ...]。"""
    hits = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            # 不碰 assets/ 下任何文件
            if _in_assets(rel):
                continue
            matched = _match_rule(fn.lower())
            if not matched:
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            hits.append((rel, size, matched[0], matched[1]))
    # 稳定排序，便于 --list 输出可复现
    hits.sort(key=lambda h: h[0].lower())
    return hits


def _print_reminder():
    print("=" * 60)
    print("回归提醒：裁剪后请实测以下功能，异常时重新构建产物即可恢复：")
    print("  1) 音频播放（.m4a / .flac / .ogg）")
    print("  2) 摄像头拍照")
    print("  3) 截屏")
    print("  4) 录音 + 转写")
    print("=" * 60)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="裁剪打包产物中无人引用的幽灵文件（默认 dry-run，不加 --apply 不会删除任何文件）。")
    parser.add_argument("dist_dir",
                        help="产物目录，如 dist\\AIRecorder 或 dist\\main.dist")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="实际删除命中文件（默认只预览）")
    mode.add_argument("--list", action="store_true",
                      help="只列出命中文件，不做任何修改")
    args = parser.parse_args(argv)

    root = args.dist_dir
    if not os.path.isdir(root):
        print(f"[ERROR] 目录不存在：{root}", file=sys.stderr)
        return 2
    if not _looks_like_dist(root):
        print(f"[ERROR] {root} 看起来不是打包产物目录"
              f"（未找到顶层 .exe 或 _internal 目录），已中止。", file=sys.stderr)
        return 2

    root = os.path.abspath(root)
    if args.apply:
        mode_text = "APPLY（实际删除）"
    elif args.list:
        mode_text = "LIST（只列出命中清单）"
    else:
        mode_text = "DRY-RUN（仅预览，不删除任何文件）"

    print("=" * 60)
    print(" 打包产物幽灵文件裁剪 (trim_dist.py)")
    print("=" * 60)
    print(f"扫描目录 : {root}")
    print(f"运行模式 : {mode_text}")

    hits = _collect_hits(root)

    if not hits:
        print("-" * 60)
        print("未发现可裁剪的幽灵文件。")
        _print_reminder()
        return 0

    if not args.list and not args.apply:
        print("提示：当前为 DRY-RUN，仅预览；如需真正删除请追加 --apply。")
    print("-" * 60)

    total = 0
    for rel, size, pattern, _reason in hits:
        total += size
        print("  {:>10.2f} MB   {:<55} [{}]".format(size / MB, rel, pattern))

    print("-" * 60)
    print(f"命中文件数 : {len(hits)}")
    print(f"命中总大小 : {total / MB:.2f} MB")

    if args.apply:
        deleted = 0
        failed = 0
        for rel, _size, _pattern, _reason in hits:
            full = os.path.join(root, rel)
            try:
                os.remove(full)
            except OSError as exc:
                failed += 1
                print(f"[警告] 删除失败：{rel} -> {exc}")
            else:
                deleted += 1
                print(f"[删除] {rel}")
        print("-" * 60)
        print(f"删除完成：成功 {deleted} 个，失败 {failed} 个。")

    _print_reminder()
    return 0


if __name__ == "__main__":
    sys.exit(main())
