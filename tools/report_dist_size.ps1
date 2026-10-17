#Requires -Version 5.1
<#
.SYNOPSIS
  统计打包产物的体积构成，便于在任意测试机上一键量出产物内部构成、定位体积大头。

.DESCRIPTION
  递归扫描 -Path 指定目录，依次输出：
    1) 总体积（MB，保留 1 位小数）与文件总数；
    2) 按子目录聚合的体积排名（前 20，目录相对 -Path，根目录记为 "(root)"）；
    3) 最大的 N 个文件及各自 MB（N 由 -Top 指定）。
  可选 -Csv 把完整文件清单写入 CSV，便于在 Excel 中进一步分析。

  说明：本脚本为只读扫描，不会修改任何文件。适用于 Nuitka / PyInstaller 等
  打包器的输出目录（如 dist\AIRecorder、dist\main.dist）。

.PARAMETER Path
  要扫描的目录，默认 dist\AIRecorder。

.PARAMETER Top
  最大文件列表的条数，默认 30。

.PARAMETER Csv
  可选。把完整文件清单写入该 CSV 路径（相对路径按当前目录解析）。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\report_dist_size.ps1 -Path dist\AIRecorder

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\report_dist_size.ps1 -Path dist\AIRecorder -Top 50 -Csv dist_size.csv
#>
[CmdletBinding()]
param(
    [string]$Path = "dist\AIRecorder",
    [int]$Top = 30,
    [string]$Csv
)

# 控制台与重定向输出统一 UTF-8，避免中文乱码
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# 以不变文化的 "N1" 格式输出 MB，避免受系统区域小数点差异影响
function Format-MB {
    param([double]$Bytes)
    return [string]::Format(
        [System.Globalization.CultureInfo]::InvariantCulture, "{0:N1}", ($Bytes / 1MB))
}

if ($Top -lt 1) { $Top = 1 }

# 校验目标目录
if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
    Write-Host "[ERROR] 目录不存在: $Path"
    exit 1
}
$root = (Resolve-Path -LiteralPath $Path).Path

Write-Host "============================================================"
Write-Host " 打包产物体积报告"
Write-Host "============================================================"

# 递归收集文件（含隐藏文件）
$files = @(Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue)
$totalBytes = 0.0
foreach ($f in $files) { $totalBytes += $f.Length }
$fileCount = $files.Count

Write-Host ("扫描目录 : {0}" -f $root)
Write-Host ("总体积   : {0} MB" -f (Format-MB $totalBytes))
Write-Host ("文件总数 : {0}" -f $fileCount)
Write-Host ""

if ($fileCount -eq 0) {
    Write-Host "目录为空，无内容可统计。"
    exit 0
}

# ---- 按子目录聚合 ----
$dirGroups = $files | Group-Object -Property {
    $dir = Split-Path -Parent $_.FullName
    if ($dir.Length -le $root.Length) { "(root)" }
    else { $dir.Substring($root.Length + 1) }
}
$dirRows = @()
foreach ($g in $dirGroups) {
    $sum = 0.0
    foreach ($f in $g.Group) { $sum += $f.Length }
    $dirRows += [PSCustomObject]@{
        Directory = $g.Name
        Bytes     = $sum
        FileCount = $g.Group.Count
    }
}
$dirRows = @($dirRows | Sort-Object -Property Bytes -Descending | Select-Object -First 20)

Write-Host "按子目录聚合（前 20，单位 MB）:"
$rank = 0
foreach ($row in $dirRows) {
    $rank++
    Write-Host ("  {0,2}. {1,10} MB   文件 {2,5}   {3}" -f $rank, (Format-MB $row.Bytes), $row.FileCount, $row.Directory)
}
Write-Host ""

# ---- 最大的 N 个文件 ----
$topFiles = @($files | Sort-Object -Property Length -Descending | Select-Object -First $Top)
Write-Host ("最大的 {0} 个文件（单位 MB）:" -f $topFiles.Count)
$rank = 0
foreach ($f in $topFiles) {
    $rank++
    $rel = $f.FullName.Substring($root.Length + 1)
    Write-Host ("  {0,3}. {1,10} MB   {2}" -f $rank, (Format-MB $f.Length), $rel)
}
Write-Host ""

# ---- 可选 CSV 导出（完整清单）----
if ($Csv) {
    $csvFull = $Csv
    if (-not [System.IO.Path]::IsPathRooted($csvFull)) {
        $csvFull = Join-Path (Get-Location).Path $csvFull
    }
    $csvDir = Split-Path -Parent $csvFull
    if ($csvDir -and -not (Test-Path -LiteralPath $csvDir)) {
        New-Item -ItemType Directory -Path $csvDir -Force | Out-Null
    }
    $records = foreach ($f in ($files | Sort-Object -Property Length -Descending)) {
        [PSCustomObject]@{
            "相对路径" = $f.FullName.Substring($root.Length + 1)
            "大小MB"   = [math]::Round($f.Length / 1MB, 3)
            "字节"     = $f.Length
            "修改时间" = $f.LastWriteTime
        }
    }
    $records | Export-Csv -LiteralPath $csvFull -NoTypeInformation -Encoding UTF8
    Write-Host ("完整清单已写入 CSV: {0}" -f $csvFull)
}

exit 0
