#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包产物「悬空依赖」审计脚本（stdlib only，Windows/Linux 通用）。

用途：扫描 PyInstaller / Nuitka 产物目录中的 PE 文件（.dll/.pyd/.exe），
用 ASCII 字符串匹配近似还原导入表的 DLL 依赖，找出「被引用但在产物目录里
不存在」的 DLL。用于验证裁剪清单没有删掉仍被依赖的文件（悬空依赖会导致
运行时插件/库加载失败）。

注意：这是近似审计——匹配的是文件内的 ASCII 字符串，会同时拾取错误消息、
延迟加载等场合出现的名称，因此报告为「可疑」而非绝对错误；系统 DLL 与
已知白名单会被过滤。用法：

    python tools/audit_dist_deps.py <产物目录>          # 如 dist/AIRecorder
    python tools/audit_dist_deps.py <目录> -v          # 显示每个文件的全部可疑项
"""

import os
import re
import sys

_NAME_RE = re.compile(rb"([A-Za-z0-9_\-\.\+]{3,60}\.(?:dll|pyd|so))")
_PE_EXT = ('.dll', '.pyd', '.exe', '.so')

# 系统 DLL / 运行时 SHA 前缀（Windows 自带，不属于产物内容）
_SYSTEM_PREFIXES = (
    'api-ms-win-', 'ext-ms-win-', 'ucrtbase', 'vcruntime', 'msvcp', 'msvcr',
    'concrt', 'vcamp', 'vccorlib', 'vcomp',
)
_SYSTEM_NAMES = {
    'kernel32.dll', 'kernelbase.dll', 'user32.dll', 'gdi32.dll', 'gdi32full.dll',
    'advapi32.dll', 'shell32.dll', 'shlwapi.dll', 'ole32.dll', 'oleaut32.dll',
    'comdlg32.dll', 'combase.dll', 'ws2_32.dll', 'wsock32.dll', 'crypt32.dll',
    'bcrypt.dll', 'ncrypt.dll', 'secur32.dll', 'sechost.dll', 'sspicli.dll',
    'ntdll.dll', 'rpcrt4.dll', 'userenv.dll', 'psapi.dll', 'setupapi.dll',
    'cfgmgr32.dll', 'wintrust.dll', 'winhttp.dll', 'wininet.dll', 'mpr.dll',
    'netapi32.dll', 'powrprof.dll', 'iphlpapi.dll', 'dnsapi.dll', 'dwmapi.dll',
    'uxtheme.dll', 'winmm.dll', 'version.dll', 'imm32.dll', 'msimg32.dll',
    'opengl32.dll', 'glu32.dll', 'gdiplus.dll', 'd2d1.dll', 'd3d11.dll',
    'dxgi.dll', 'dwrite.dll', 'd3d9.dll', 'hid.dll', 'avicap32.dll',
    'msvcrt.dll', 'oleacc.dll', 'propsys.dll', 'wtsapi32.dll', 'cryptbase.dll',
    'secur32.dll', 'sspicli.dll', 'imagehlp.dll', 'dbghelp.dll', 'wevtapi.dll',
    'winspool.drv', 'mswsock.dll', 'netutils.dll', 'srvcli.dll', 'wkscli.dll',
    'normaliz.dll', 'urlmon.dll', 'windowscodecs.dll', 'wldap32.dll',
    'dsound.dll', 'mfplat.dll', 'mf.dll', 'mfreadwrite.dll', 'mfuuid.dll',
    'mpr.dll', 'samcli.dll', 'davclnt.dll', 'msasn1.dll', 'gpapi.dll',
    'ktmw32.dll', 'winsta.dll', 'apphelp.dll', 'api-ms-win-core-*.dll',
}


def _is_system(name):
    if name in _SYSTEM_NAMES:
        return True
    return any(name.startswith(p) for p in _SYSTEM_PREFIXES)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    root = argv[1]
    verbose = '-v' in argv
    if not os.path.isdir(root):
        print(f'目录不存在：{root}')
        return 2

    # 产物内现有文件（小写 basename 集合）
    present = set()
    targets = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            present.add(fn.lower())
            if fn.lower().endswith(_PE_EXT):
                targets.append(os.path.join(dirpath, fn))

    print(f'扫描 {len(targets)} 个 PE 文件（目录内共 {len(present)} 个文件）…')
    dangling = 0
    for path in sorted(targets):
        try:
            with open(path, 'rb') as fh:
                blob = fh.read()
        except OSError as exc:
            print(f'[跳过] {path}: {exc}')
            continue
        refs = set()
        for m in _NAME_RE.finditer(blob):
            name = m.group(1).decode('ascii', 'replace').lower()
            if name == os.path.basename(path).lower():
                continue
            if name in present or _is_system(name):
                continue
            refs.add(name)
        if not refs:
            continue
        rel = os.path.relpath(path, root)
        shown = sorted(refs) if verbose else sorted(refs)[:6]
        suffix = '' if verbose or len(refs) <= 6 else f' …共 {len(refs)} 项'
        print(f'[可疑] {rel} -> {", ".join(shown)}{suffix}')
        dangling += len(refs)

    print(f'完成：可疑悬空引用 {dangling} 条。'
          '若某条目确实需要，请把它加回产物（通常是插件与它依赖的 Qt6*.dll 成对出现）。')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
