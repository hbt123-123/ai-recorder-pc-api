<#
.SYNOPSIS
    下载 UPX 预编译包到 tools/（build.spec 会自动发现并使用）。

.DESCRIPTION
    PyInstaller 仅当 PATH（或 spec 注入的目录）里能找到 upx.exe 时才压缩产物；
    找不到会「静默跳过」——产物直接大出 100MB+ 且没有任何报错（本仓库在
    2026-10 排查到的最大一块体积浪费就来自这里）。

    build.spec 已自动探测 tools/upx-*/upx.exe（取版本号最大者），本脚本负责
    从 UPX 官方 GitHub Release 拉取 win64 预编译包（纯二进制，不需要任何
    C++ 工具链）并解压到位。PyInstaller 侧构建前跑一次即可。

    Nuitka 侧：standalone 产物默认不压缩，构建后可用
    tools\upx_compress_dist.ps1 对产物内的 DLL/PYD 做事后压缩。

.PARAMETER Version
    指定 UPX 版本号（如 5.2.1）；默认 latest（GitHub 最新 Release）。

.PARAMETER Force
    目录里已存在 UPX 时也重新下载解压。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\fetch_upx.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\fetch_upx.ps1 -Version 5.2.1 -Force
#>
[CmdletBinding()]
param(
    [string]$Version = 'latest',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$toolsDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Get-UpxExe {
    <# 在 tools/ 下查找 upx-*/upx.exe，返回第一个（目录名排序） #>
    $found = Get-ChildItem -Path $toolsDir -Directory -Filter 'upx-*' -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'upx.exe' } |
        Where-Object { Test-Path $_ }
    return ($found | Select-Object -First 1)
}

$existing = Get-UpxExe
if ($existing -and -not $Force) {
    Write-Host "[fetch_upx] tools/ 下已存在 UPX，跳过下载（需要更新请加 -Force）：$existing"
    & $existing --version | Select-Object -First 1
    exit 0
}

$api = if ($Version -eq 'latest') {
    'https://api.github.com/repos/upx/upx/releases/latest'
} else {
    "https://api.github.com/repos/upx/upx/releases/tags/v$($Version.TrimStart('v'))"
}

Write-Host "[fetch_upx] 查询 UPX Release：$api"
$ProgressPreference = 'SilentlyContinue'
$release = Invoke-RestMethod -Uri $api -TimeoutSec 60
$asset = $release.assets | Where-Object { $_.name -match 'win64\.zip$' } | Select-Object -First 1
if (-not $asset) {
    throw "[fetch_upx] 未在 $($release.tag_name) 中找到 win64.zip 资产，请手动下载：https://github.com/upx/upx/releases"
}

$zip = Join-Path $toolsDir 'upx.zip'
Write-Host ("[fetch_upx] 下载 {0}（{1:N2} MB）…" -f $asset.name, ($asset.size / 1MB))
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -TimeoutSec 300

Write-Host "[fetch_upx] 解压到 $toolsDir …"
Expand-Archive -Path $zip -DestinationPath $toolsDir -Force
Remove-Item $zip -Force

$upx = Get-UpxExe
if (-not $upx) {
    throw "[fetch_upx] 解压完成但未找到 upx.exe，请检查 $toolsDir 目录内容。"
}
Write-Host "[fetch_upx] 完成：$upx"
& $upx --version | Select-Object -First 1
Write-Host "[fetch_upx] 现在可直接构建：pyinstaller build.spec（spec 会自动启用该 UPX 并打印确认行）。"
