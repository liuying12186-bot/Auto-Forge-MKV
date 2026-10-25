# Auto-Forge-MKV / build.ps1
#
# 一键打包成单文件 exe。
#     powershell -ExecutionPolicy Bypass -File build.ps1
#
# 产物：dist\AutoForgeMKV.exe
#
# 注意：ffmpeg / ffprobe / mkvmerge 不打包进去。
# 它们加起来上百 MB，塞进 exe 每次启动都要解包，反而更慢。
# 运行时从 PATH 或 MKVToolNix 默认安装位置查找。

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host '== 检查依赖 ==' -ForegroundColor Cyan
$missing = @()
foreach ($m in @('PyInstaller')) {
    $ok = $null -ne (python -m pip show $m 2>$null)
    if (-not $ok) { $missing += $m }
}
if ($missing.Count -gt 0) {
    Write-Host ("缺少: " + ($missing -join ', ') + '，正在安装…') -ForegroundColor Yellow
    python -m pip install --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller
}

Write-Host '== 跑测试 ==' -ForegroundColor Cyan
python -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw '测试未通过，停止打包' }

Write-Host '== PyInstaller ==' -ForegroundColor Cyan
python -m PyInstaller forge.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw '打包失败' }

$exe = Join-Path $PSScriptRoot 'dist\AutoForgeMKV.exe'
if (-not (Test-Path $exe)) { throw '没找到产物' }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 2)
Write-Host ("== 完成: " + $exe + " (" + $size + " MB) ==") -ForegroundColor Green
Write-Host '自检：dist\AutoForgeMKV.exe --help'
