# datacompare 离线安装脚本（Windows / PowerShell）
#
# 在**完全离线**的机器上执行：
#     powershell -ExecutionPolicy Bypass -File install_offline.ps1
#
# 可选参数：
#     -PythonExe "C:\Python312\python.exe"   指定解释器
#     -VenvDir   "C:\datacompare\venv"       虚拟环境目录
#     -InstallExtensions                     把 DuckDB 扩展装到用户目录

[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [string]$VenvDir   = "$PSScriptRoot\venv",
    [switch]$InstallExtensions
)

$ErrorActionPreference = "Stop"
$Here   = $PSScriptRoot
$Wheels = Join-Path $Here "wheels"

Write-Host "==> datacompare 离线安装" -ForegroundColor Cyan
Write-Host "    安装包：$Here"

# ---------- 1. 找一个可用的 Python ----------
function Find-Python {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "指定的 Python 不存在：$Explicit" }
        return $Explicit
    }
    # PATH 里的 python / py 启动器
    foreach ($cmd in @("python", "python3")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    $py = Get-Command "py" -ErrorAction SilentlyContinue
    if ($py) { return "$($py.Source) -3" }
    # 常见安装位置兜底
    foreach ($v in @("313", "312", "311", "310", "39")) {
        foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "C:\")) {
            $cand = Join-Path $root "Python$v\python.exe"
            if (Test-Path $cand) { return $cand }
        }
    }
    return ""
}

$py = Find-Python -Explicit $PythonExe

# ---------- 1.5 兜底：使用包内自带的 Python 安装器（离线机器没装 Python 时） ----------
if (-not $py) {
    $installer = Get-ChildItem $Here -Filter "python-*-amd64.exe" -ErrorAction SilentlyContinue |
                 Sort-Object Name -Descending | Select-Object -First 1
    if ($installer) {
        $target = Join-Path $Here "python"
        Write-Host "==> 未找到 Python，使用包内自带安装器（$($installer.Name)）静默安装到 $target" -ForegroundColor Yellow
        $installArgs = @(
            "/quiet", "InstallAllUsers=0", "PrependPath=0",
            "Include_venv=1", "Include_pip=1", "Include_test=0", "Include_launcher=0",
            "TargetDir=$target"
        )
        Start-Process -FilePath $installer.FullName -ArgumentList $installArgs -Wait | Out-Null
        $bundled = Join-Path $target "python.exe"
        if (Test-Path $bundled) {
            $py = $bundled
            Write-Host "    已安装：$bundled" -ForegroundColor Green
        }
    }
}

if (-not $py) {
    Write-Host "错误：找不到 Python 解释器，且包内没有自带安装器。" -ForegroundColor Red
    Write-Host "      请改装 Python 3.9+，或用 -PythonExe 指定解释器；" -ForegroundColor Red
    Write-Host "      也可以改用 PyInstaller 方案（见 docs\DEPLOY.md 方案 C，无需 Python）。" -ForegroundColor Red
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

if (-not (Test-Path $Wheels)) {
    Write-Host "错误：找不到 wheels 目录（$Wheels）" -ForegroundColor Red
    exit 1
}

# ---------- 2. 建虚拟环境并离线安装 ----------
Write-Host "==> 创建虚拟环境：$VenvDir"
& $py @pyArgs -m venv $VenvDir

$pip = Join-Path $VenvDir "Scripts\pip.exe"
Write-Host "==> 离线安装依赖（不访问任何网络）"
& $pip install --quiet --upgrade pip 2>$null
& $pip install --no-index --find-links $Wheels --disable-pip-version-check datacompare
Write-Host "==> 离线安装完成"

# ---------- 3. 可选：安装 DuckDB 扩展 ----------
$extSrc = Join-Path $Here "duckdb_extensions"
if ((Test-Path $extSrc) -and $InstallExtensions) {
    $extDest = Join-Path $env:USERPROFILE ".duckdb\extensions"
    Write-Host "==> 安装 DuckDB 扩展到 $extDest"
    # duckdb_extensions 内部结构镜像 extension_directory：<版本>\<平台>\*.duckdb_extension
    New-Item -ItemType Directory -Force -Path $extDest | Out-Null
    Copy-Item "$extSrc\*" $extDest -Recurse -Force
    Get-ChildItem $extSrc -Recurse -Filter "*.duckdb_extension" | ForEach-Object {
        Write-Host ("    " + $_.FullName.Substring($extSrc.Length + 1))
    }
}

# ---------- 4. 自检 ----------
Write-Host "==> 自检"
$exe = Join-Path $VenvDir "Scripts\datacompare.exe"
& $exe --version
$pyExe = Join-Path $VenvDir "Scripts\python.exe"
& $pyExe -c @"
import duckdb, openpyxl, yaml
print('    duckdb   ' + duckdb.__version__)
print('    openpyxl ' + openpyxl.__version__)
print('    PyYAML   ' + yaml.__version__)
"@

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
Write-Host ""
Write-Host "用法："
Write-Host "    & '$exe' compare -b 旧数据.csv -a 新数据.csv -k 单据号 -o 输出目录"
Write-Host ""
Write-Host "临时加到当前会话 PATH："
Write-Host "    `$env:PATH = '$VenvDir\Scripts;' + `$env:PATH"
