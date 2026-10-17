#Requires -Version 5.1
<#
.SYNOPSIS
  对 Nuitka standalone 产物中的 DLL / PYD 执行 UPX 事后压缩。

.DESCRIPTION
  背景：PyInstaller 的 UPX 只作用于它自己收集的二进制，而 Nuitka standalone
  输出完全未压缩；因此本脚本用于给 Nuitka 产物做事后压缩，可显著缩减体积。

  行为：
    - 递归扫描 -Path 下的 *.dll 与 *.pyd；
    - 有意排除 *.exe（主程序 exe 不压缩：Nuitka/PyInstaller 的 exe 经 UPX
      压缩后可能无法启动）；
    - 跳过排除清单内的系统/运行时关键 DLL（见脚本内 $ExcludePatterns）；
    - 跳过已经用 UPX 压过的文件（读文件头前 200KB，含 "UPX!" 魔数即视为已压缩）；
    - 对每个目标执行 `upx --best <file>`；单个文件失败只警告、不终止；
    - 带 CFG（Control Flow Guard / GUARD_CF）的 PE 文件会被 UPX 拒绝压缩
      （CantPackException: GUARD_CF enabled PE files are not supported），
      默认模式下这类文件单独计入「CFG 跳过」统计，不算失败；
    - 结束后打印汇总（压缩前总计、压缩后总计、节省 MB/百分比、成功/CFG 跳过/失败三档统计）。

  注意：UPX 可能触发个别杀软误报。如遇启动异常，删除本脚本的压缩结果
  （重新打包即可——本脚本是在打包产物上原地修改，重跑构建会生成新的未压缩产物）。

.PARAMETER Path
  要压缩的产物目录，默认 dist\main.dist。

.PARAMETER UpxExe
  upx.exe 路径，默认 tools\upx-5.2.1-win64\upx.exe；若该路径不存在，则在
  系统 PATH 中查找 upx。

.PARAMETER DryRun
  只列出将被压缩的文件与预计总计，不实际压缩、不修改任何文件。

.PARAMETER Force
  实验性开关（默认关闭）。开启后给 upx 追加 `--force`，从而强制压缩带 CFG 的
  PE 文件（收益显著，例如 Qt6Core.dll 可由约 9.88MB 压到约 3.33MB）。
  风险：强制压缩会绕过 UPX 对 GUARD_CF 的保护，可能导致程序启动崩溃（CFG
  守护表失效），且杀软误报概率上升。仅建议在确认可完整回归的机器上使用。
  启用后务必完整回归：启动、音频播放（m4a/flac/ogg）、摄像头、截屏、录音转写。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\upx_compress_dist.ps1 -Path dist\main.dist

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\upx_compress_dist.ps1 -Path dist\AIRecorder -DryRun

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\upx_compress_dist.ps1 -Path dist\AIRecorder -Force
#>
[CmdletBinding()]
param(
    [string]$Path = "dist\main.dist",
    [string]$UpxExe = "tools\upx-5.2.1-win64\upx.exe",
    [switch]$DryRun,
    [switch]$Force
)

# 控制台与重定向输出统一 UTF-8，避免中文乱码
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# 以不变文化的 "N1" 格式输出 MB，避免受系统区域小数点差异影响
function Format-MB {
    param([double]$Bytes)
    return [string]::Format(
        [System.Globalization.CultureInfo]::InvariantCulture, "{0:N1}", ($Bytes / 1MB))
}

# 排除清单：系统/运行时关键 DLL，压缩后可能影响加载或本身不可压缩
$ExcludePatterns = @(
    'python3*.dll',
    'vcruntime*',
    'msvcp*',
    'concrt*',
    'vcamp*',
    'vccorlib*',
    'vcomp*',
    'ucrtbase.dll',
    'api-ms-win-*.dll'
)

# 读文件头前 200KB 判断是否已被 UPX 压缩
function Test-UpxPacked {
    param([string]$FilePath)
    try {
        $fs = [System.IO.File]::OpenRead($FilePath)
        try {
            $buf = New-Object byte[] 204800
            $read = $fs.Read($buf, 0, $buf.Length)
        }
        finally { $fs.Dispose() }
    }
    catch { return $false }
    if ($read -le 0) { return $false }
    # 以 Latin1 把每个字节映射为一个字符，再查找 ASCII "UPX!" 魔数
    $text = [System.Text.Encoding]::GetEncoding(28591).GetString($buf, 0, $read)
    return ($text.IndexOf("UPX!") -ge 0)
}

# 定位 upx 可执行文件
$upxPath = $null
if ($UpxExe -and (Test-Path -LiteralPath $UpxExe -PathType Leaf)) {
    $upxPath = (Resolve-Path -LiteralPath $UpxExe).Path
}
else {
    $cmd = Get-Command -Name "upx" -ErrorAction SilentlyContinue
    if ($cmd) { $upxPath = $cmd.Source }
}

Write-Host "============================================================"
Write-Host " Nuitka 产物 UPX 事后压缩"
Write-Host "============================================================"

if ($Force) {
    Write-Host ""
    Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    Write-Host "!! [警告] 已启用 -Force：实验性强制压缩 CFG 二进制        !!"
    Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    Write-Host "  本模式会给 upx 追加 --force，强制压缩带 CFG（GUARD_CF）"
    Write-Host "  的 PE 文件。收益显著，但存在以下风险："
    Write-Host "    1) 可能绕过 CFG 保护导致进程启动崩溃（守护表失效）；"
    Write-Host "    2) 被杀软误报的概率明显上升；"
    Write-Host "    3) 属实验性用法，不受上游 UPX 支持。"
    Write-Host "  强烈建议只在确认可完整回归的机器上使用；启用后务必完整回归："
    Write-Host "    启动、音频播放（m4a/flac/ogg）、摄像头、截屏、录音转写。"
    Write-Host "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    Write-Host ""
}

if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
    Write-Host "[ERROR] 目录不存在: $Path"
    exit 1
}
$root = (Resolve-Path -LiteralPath $Path).Path

if (-not $DryRun -and -not $upxPath) {
    Write-Host "[ERROR] 未找到 upx.exe（既不在 -UpxExe 指定路径，也不在 PATH 中）"
    Write-Host "        请用 -UpxExe 指定路径，或把 upx 加入 PATH。"
    exit 1
}

Write-Host ("扫描目录 : {0}" -f $root)
if ($upxPath) { Write-Host ("UPX 程序 : {0}" -f $upxPath) }
if ($DryRun) { Write-Host "运行模式 : DryRun（仅列出，不压缩）" }
elseif ($Force) { Write-Host "运行模式 : 实际压缩（upx --best --force，实验性）" }
else { Write-Host "运行模式 : 实际压缩（upx --best）" }
Write-Host ""

# 收集候选：仅 *.dll / *.pyd（有意排除 *.exe）
$allFiles = @(Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue)
$candidates = @()
$skippedExclude = 0
$skippedPacked = 0

foreach ($f in $allFiles) {
    $ext = $f.Extension.ToLowerInvariant()
    if ($ext -ne ".dll" -and $ext -ne ".pyd") { continue }

    $excluded = $false
    foreach ($pat in $ExcludePatterns) {
        if ($f.Name -like $pat) { $excluded = $true; break }
    }
    if ($excluded) { $skippedExclude++; continue }

    if (Test-UpxPacked -FilePath $f.FullName) { $skippedPacked++; continue }

    $candidates += $f
}

$totalBefore = 0.0
foreach ($f in $candidates) { $totalBefore += $f.Length }

Write-Host ("候选文件数   : {0}（*.dll / *.pyd）" -f $candidates.Count)
Write-Host ("排除清单跳过 : {0} 个（python3*/vcruntime*/msvcp*/ucrtbase/api-ms-win-* 等）" -f $skippedExclude)
Write-Host ("已是 UPX 跳过 : {0} 个" -f $skippedPacked)
Write-Host ("压缩前总计   : {0} MB" -f (Format-MB $totalBefore))
Write-Host ""

if ($candidates.Count -eq 0) {
    Write-Host "没有需要压缩的文件。"
    exit 0
}

if ($DryRun) {
    Write-Host "将压缩以下文件（DryRun，未实际执行）:"
    foreach ($f in $candidates) {
        $rel = $f.FullName.Substring($root.Length + 1)
        Write-Host ("  {0,10} MB   {1}" -f (Format-MB $f.Length), $rel)
    }
    Write-Host ""
    Write-Host ("预计压缩前总计: {0} MB" -f (Format-MB $totalBefore))
    Write-Host "DryRun 完成：未修改任何文件。"
    exit 0
}

# ---- 实际压缩 ----
$sumBefore = 0.0
$sumAfter = 0.0
$failures = @()
$cfgSkipped = @()
$successCount = 0
$index = 0

# 组装 upx 参数：默认 `--best`；-Force 时追加 `--force` 以强制压缩 CFG 二进制
$upxArgs = @('--best')
if ($Force) { $upxArgs += '--force' }

foreach ($f in $candidates) {
    $index++
    $rel = $f.FullName.Substring($root.Length + 1)
    $sizeBefore = $f.Length
    Write-Host ("[{0}/{1}] {2}  ({3} MB)" -f $index, $candidates.Count, $rel, (Format-MB $sizeBefore))

    $out = & $upxPath @upxArgs $f.FullName 2>&1
    $code = $LASTEXITCODE

    $sizeAfter = $sizeBefore
    try { $sizeAfter = (Get-Item -LiteralPath $f.FullName).Length } catch { }

    $sumBefore += $sizeBefore
    $sumAfter += $sizeAfter

    if ($code -eq 0) {
        $successCount++
        Write-Host ("          压缩前 {0} MB -> 压缩后 {1} MB" -f (Format-MB $sizeBefore), (Format-MB $sizeAfter))
    }
    else {
        $outText = ''
        if ($out) { $outText = (@($out) -join "`n") }
        # 带 CFG 的 PE 会被 UPX 拒绝：CantPackException / GUARD_CF
        $isCfg = ($outText -match 'GUARD_CF') -or ($outText -match 'CantPackException')
        if ($isCfg) {
            $cfgSkipped += $rel
            Write-Host "          [SKIP] 该 PE 带 CFG（GUARD_CF），UPX 默认拒绝压缩，已跳过"
        }
        else {
            $failures += $rel
            Write-Host ("          [WARN] upx 退出码 {0}，已跳过该文件" -f $code)
        }
        if ($out) {
            $msg = (@($out) | Select-Object -Last 3) -join " / "
            Write-Host ("          {0}" -f $msg)
        }
    }
}

# ---- 汇总 ----
$saved = $sumBefore - $sumAfter
$pct = 0.0
if ($sumBefore -gt 0) { $pct = ($saved / $sumBefore) * 100.0 }
$pctText = [string]::Format([System.Globalization.CultureInfo]::InvariantCulture, "{0:N1}", $pct)

Write-Host ""
Write-Host "============================================================"
Write-Host " 压缩汇总"
Write-Host "============================================================"
Write-Host ("压缩前总计 : {0} MB" -f (Format-MB $sumBefore))
Write-Host ("压缩后总计 : {0} MB" -f (Format-MB $sumAfter))
Write-Host ("节省       : {0} MB ({1} %)" -f (Format-MB $saved), $pctText)
Write-Host ""
Write-Host ("成功压缩   : {0} 个（节省 {1} MB）" -f $successCount, (Format-MB $saved))
Write-Host ("CFG 跳过   : {0} 个{1}" -f $cfgSkipped.Count, $(if (-not $Force) { "（未启用 -Force）" } else { "" }))
Write-Host ("失败(非CFG): {0} 个" -f $failures.Count)
if ($cfgSkipped.Count -gt 0) {
    Write-Host ""
    Write-Host "CFG 跳过清单（带 GUARD_CF，UPX 默认拒绝压缩）:"
    foreach ($rel in $cfgSkipped) { Write-Host ("  - {0}" -f $rel) }
    Write-Host "  说明：以上文件可用 -Force 开关强制压缩（upx --force），收益显著但属实验性；"
    Write-Host "        启用后务必完整回归：启动、音频播放（m4a/flac/ogg）、摄像头、截屏、录音转写。"
}
if ($failures.Count -gt 0) {
    Write-Host ""
    Write-Host "失败清单（非 CFG 原因）:"
    foreach ($rel in $failures) { Write-Host ("  - {0}" -f $rel) }
}

exit 0
