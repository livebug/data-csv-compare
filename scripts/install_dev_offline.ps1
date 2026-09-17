# datacompare 内网二次开发环境安装（Windows / PowerShell）
#
# 与 install_offline.ps1 的区别：
#   install_offline.ps1      只装 wheel，装完能跑，改不了代码
#   install_dev_offline.ps1  可编辑安装 source\，改代码立刻生效，还能跑测试
#
# 在**完全离线**的机器上执行：
#     powershell -ExecutionPolicy Bypass -File install_dev_offline.ps1
#
# 可选参数：
#     -PythonExe "C:\Python312\python.exe"   指定解释器
#     -VenvDir   "C:\datacompare\venv-dev"   虚拟环境目录
#     -SourceDir "D:\my-fork"                源码目录（可换成你自己的仓库）
#     -SkipTests                             跳过 pytest 自检
#     -InstallExtensions                     把 DuckDB 扩展装到用户目录

[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [string]$VenvDir   = "$PSScriptRoot\venv-dev",
    [string]$SourceDir = "$PSScriptRoot\source",
    [string]$WheelsDir = "$PSScriptRoot\wheels",
    [switch]$SkipTests,
    [switch]$InstallExtensions
)

$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot

Write-Host "==> datacompare 内网二次开发环境" -ForegroundColor Cyan
Write-Host "    安装包：$Here"
Write-Host "    虚拟环境：$VenvDir"

# ---------- 1. 找一个可用的 Python ----------
function Find-Python {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "指定的 Python 不存在：$Explicit" }
        return $Explicit
    }
    foreach ($cmd in @("python", "python3")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    $py = Get-Command "py" -ErrorAction SilentlyContinue
    if ($py) { return "$($py.Source) -3" }
    foreach ($v in @("313", "312", "311", "310", "39")) {
        foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "C:\")) {
            $cand = Join-Path $root "Python$v\python.exe"
            if (Test-Path $cand) { return $cand }
        }
    }
    return ""
}

$py = Find-Python -Explicit $PythonExe
if (-not $py) {
    Write-Host "错误：找不到 Python 解释器，本方案要求目标机器已装 Python 3.9+。" -ForegroundColor Red
    exit 1
}
Write-Host "==> 使用解释器：$py"

$pyArgs = @()
if ($py -like "* -3") { $pyArgs = @("-3"); $py = $py -replace " -3$", "" }
$verOutput = & $py @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "    Python $verOutput"
if ([version]$verOutput -lt [version]"3.9") {
    Write-Host "错误：需要 Python 3.9 或更高版本" -ForegroundColor Red
    exit 1
}

# ---------- 2. 目录检查 ----------
if (-not (Test-Path $WheelsDir)) {
    Write-Host "错误：找不到 wheels 目录（$WheelsDir）" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $SourceDir)) {
    Write-Host "错误：找不到源码目录（$SourceDir）" -ForegroundColor Red
    Write-Host "      用 --no-source 生成的包不含源码，无法二次开发；请重新制作。" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $SourceDir "pyproject.toml"))) {
    Write-Host "错误：$SourceDir 看起来不是本项目源码（缺 pyproject.toml）" -ForegroundColor Red
    exit 1
}
$devReq = Join-Path $SourceDir "requirements-dev.txt"
$hasDevReq = Test-Path $devReq

# ---------- 3. 建虚拟环境 ----------
Write-Host "==> 创建虚拟环境：$VenvDir"
& $py @pyArgs -m venv $VenvDir
$pip = Join-Path $VenvDir "Scripts\pip.exe"
$pyExe = Join-Path $VenvDir "Scripts\python.exe"

# ---------- 4. 离线安装（全程 --no-index，绝不联网） ----------
Write-Host "==> 升级 pip（离线，用包里的 pip wheel）"
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $pip install --quiet --no-index --find-links $WheelsDir --disable-pip-version-check --upgrade pip 2>&1 |
    ForEach-Object { Write-Host "    $_" }
if ($LASTEXITCODE -ne 0) {
    Write-Host "    跳过：包内没有 pip wheel，沿用虚拟环境自带的 pip" -ForegroundColor Yellow
}
$ErrorActionPreference = $prev

if ($hasDevReq) {
    Write-Host "==> 离线安装运行 + 开发依赖"
    & $pip install --no-index --find-links $WheelsDir --disable-pip-version-check -r $devReq
} else {
    Write-Host "警告：源码里没有 requirements-dev.txt，只装运行依赖" -ForegroundColor Yellow
    & $pip install --no-index --find-links $WheelsDir --disable-pip-version-check `
        -r (Join-Path $SourceDir "requirements.txt")
}

Write-Host "==> 可编辑安装源码（$SourceDir）"
# 可编辑安装要走 build isolation，pip 会从 --find-links 里拿 setuptools/wheel，
# 所以这两个 wheel 必须在包内（requirements-dev.txt 里已经列了）。
& $pip install --no-index --find-links $WheelsDir --disable-pip-version-check -e $SourceDir

# ---------- 5. 可选：安装 DuckDB 扩展 ----------
$extSrc = Join-Path $Here "duckdb_extensions"
if ((Test-Path $extSrc) -and $InstallExtensions) {
    $extDest = Join-Path $env:USERPROFILE ".duckdb\extensions"
    Write-Host "==> 安装 DuckDB 扩展到 $extDest"
    New-Item -ItemType Directory -Force -Path $extDest | Out-Null
    Copy-Item "$extSrc\*" $extDest -Recurse -Force
}

# ---------- 6. 自检 ----------
Write-Host "==> 自检"
& (Join-Path $VenvDir "Scripts\datacompare.exe") --version
& $pyExe -c @"
import duckdb, openpyxl, yaml
print('    duckdb   ' + duckdb.__version__)
print('    openpyxl ' + openpyxl.__version__)
print('    PyYAML   ' + yaml.__version__)
"@

if ($SkipTests) {
    Write-Host "==> 跳过测试（-SkipTests）"
} else {
    Write-Host "==> 跑测试（应该是全绿）"
    Push-Location $SourceDir
    try { & $pyExe -m pytest -q tests } finally { Pop-Location }
}

Write-Host ""
Write-Host "二次开发环境就绪。" -ForegroundColor Green
Write-Host ""
Write-Host "跑对比："
Write-Host "    & '$VenvDir\Scripts\datacompare.exe' compare -b 旧数据.csv -a 新数据.csv -k 单据号 -o out"
Write-Host ""
Write-Host "跑测试（源码目录下）："
Write-Host "    cd $SourceDir; & '$pyExe' -m pytest tests -q"
Write-Host ""
Write-Host "改代码不需要重装：venv 已经指向 $SourceDir\src\datacompare。"
Write-Host "重新打成 wheel（离线也能打）："
Write-Host "    cd $SourceDir; & '$pyExe' -m pip wheel --no-deps --no-build-isolation --no-index -w dist ."
