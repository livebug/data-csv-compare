<#
.SYNOPSIS
    一键构建 datacompare 的 Windows 发布物（exe 包 + Python 离线包）。

.DESCRIPTION
    在联网的 Windows 机器上运行。产出（默认 dist\release\）：
      datacompare-<ver>-windows-x64-standalone.zip
                                              解压即用（PyInstaller onedir，无需 Python）
      datacompare-<ver>-windows-x64-offline-kit.zip
                                              Windows 离线套件：自带 Python 安装器 +
                                              wheels + 源码，目标机没 Python 也能装
                                              （传入 -PythonInstaller；加 -WithSource 带源码）

.PARAMETER Version
    版本号。默认从 pyproject.toml 的 version 读取。

.PARAMETER PythonExe
    用于构建的 Python 解释器。默认在 PATH 里找 python / py。

.PARAMETER PythonInstaller
    python-3.12.x-amd64.exe 路径。提供时会放进离线包，让完全没有 Python
    的离线机器也能一键安装。

.PARAMETER IndexUrl
    pip 镜像地址，例如 https://pypi.tuna.tsinghua.edu.cn/simple。

.PARAMETER WithSource
    离线包里连源码和开发依赖一起带（供内网二次开发）。默认不带，
    只做部署包更小；需要时加这个开关。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build_windows_release.ps1 `
        -PythonInstaller C:\downloads\python-3.12.10-amd64.exe `
        -IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple
#>
[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$PythonExe = "",
    [string]$PythonInstaller = "",
    [string]$IndexUrl = "",
    [switch]$SkipExe,
    [switch]$SkipOffline,
    [switch]$WithSource
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$Root = Split-Path -Parent $PSScriptRoot
$ReleaseDir = Join-Path $Root "dist\release"
$VenvDir = Join-Path $Root ".venv-build"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

# 外部程序（pip / pyinstaller 等）会把日志写到 stderr，
# 在 $ErrorActionPreference='Stop' 下会被误判为错误，这里统一兜住。
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [Parameter(Mandatory)][string[]]$Arguments
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "    $_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) {
        throw "命令失败（exit $code）：$Exe $($Arguments -join ' ')"
    }
}

# ---------- 版本 ----------
if (-not $Version) {
    $pyproject = Get-Content (Join-Path $Root "pyproject.toml") -Raw
    if ($pyproject -match '(?m)^\s*version\s*=\s*"([^"]+)"') {
        $Version = $Matches[1]
    } else {
        throw "无法从 pyproject.toml 读取版本号，请用 -Version 指定"
    }
}
Write-Step "datacompare $Version  Windows 发布构建"

# ---------- Python ----------
function Resolve-Python {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "指定的 Python 不存在：$Explicit" }
        return (Resolve-Path $Explicit).Path
    }
    foreach ($cmd in @("python", "python3")) {
        $c = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($c) {
            $v = & $c.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($v -and ([version]$v -ge [version]"3.9")) { return $c.Source }
        }
    }
    throw "找不到 Python 3.9+，请用 -PythonExe 指定"
}

$py = Resolve-Python -Explicit $PythonExe
Write-Host "    解释器：$py"
$pyVer = & $py -c "import sys; print(sys.version.split()[0])" 2>$null
Write-Host "    版本  ：$pyVer"

$pipArgs = @()
if ($IndexUrl) { $pipArgs += @("--index-url", $IndexUrl) }

# ---------- 构建用 venv ----------
if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
    Write-Step "创建构建虚拟环境 $VenvDir"
    Invoke-Native -Exe $py -Arguments @("-m", "venv", $VenvDir)
}
$buildPy = Join-Path $VenvDir "Scripts\python.exe"

Write-Step "安装构建依赖"
Invoke-Native -Exe $buildPy -Arguments (@("-m", "pip", "install", "--quiet", "--upgrade", "pip") + $pipArgs)
Invoke-Native -Exe $buildPy -Arguments (@("-m", "pip", "install", "--quiet", "-r", (Join-Path $Root "requirements.txt"), "pyinstaller") + $pipArgs)

# ---------- zip 辅助 ----------
function Add-FileToZip {
    param($Zip, [string]$Path, [string]$EntryName)
    # PS 5.1 的 CreateEntryFromFile 默认可能不压缩，这里显式指定
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $Zip, $Path, $EntryName, [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
}
function Add-DirToZip {
    param($Zip, [string]$Dir, [string]$Prefix)
    Get-ChildItem $Dir -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($Dir.Length).TrimStart('\', '/')
        $entry = if ([string]::IsNullOrEmpty($Prefix)) { $rel } else { "$Prefix/$rel" }
        Add-FileToZip -Zip $Zip -Path $_.FullName -EntryName $entry
    }
}
function New-Zip {
    param([string]$Path)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    if (Test-Path $Path) { Remove-Item $Path -Force }
    return [System.IO.Compression.ZipFile]::Open($Path, [System.IO.Compression.ZipArchiveMode]::Create)
}

# ---------- 1. exe 包 ----------
if (-not $SkipExe) {
    Write-Step "构建 exe（PyInstaller onedir）"
    Invoke-Native -Exe $buildPy -Arguments @(
        (Join-Path $Root "scripts\build_exe.py"), "--onedir", "--clean", "--with-extensions"
    )

    $exeDir = Join-Path $Root "dist\datacompare"
    if (-not (Test-Path $exeDir)) { throw "构建产物不存在：$exeDir" }

    Write-Step "打包 exe zip"
    $exeZip = Join-Path $ReleaseDir "datacompare-$Version-windows-x64-standalone.zip"
    $zip = New-Zip $exeZip
    try {
        Add-DirToZip -Zip $zip -Dir $exeDir -Prefix "datacompare"
        foreach ($doc in @("README.md", "CHANGELOG.md", "config.example.yaml", "LICENSE")) {
            $p = Join-Path $Root $doc
            if (Test-Path $p) { Add-FileToZip -Zip $zip -Path $p -EntryName $doc }
        }
        $dep = Join-Path $Root "docs\DEPLOY.md"
        if (Test-Path $dep) { Add-FileToZip -Zip $zip -Path $dep -EntryName "DEPLOY.md" }
    } finally { $zip.Dispose() }
    Write-Host ("    " + $exeZip) -ForegroundColor Green
}

# ---------- 2. Python 离线包 ----------
if (-not $SkipOffline) {
    if ($WithSource) {
        Write-Step "构建 Python 离线包（wheels + DuckDB 扩展 + 源码）"
    } else {
        Write-Step "构建 Python 离线包（wheels + DuckDB 扩展）"
    }
    $bundleDir = Join-Path $Root "dist\offline-bundle"
    if (Test-Path $bundleDir) { Remove-Item $bundleDir -Recurse -Force }

    # build_offline_bundle.py 默认带源码与开发依赖，发布包按需二选一
    $bundleArgs = @(
        (Join-Path $Root "scripts\build_offline_bundle.py"),
        "--out", $bundleDir, "--python", "3.12", "--platforms", "win-x64", "--with-extensions"
    )
    if (-not $WithSource) { $bundleArgs += @("--no-source", "--no-dev") }

    # 用构建 venv（含 duckdb）跑，才能顺带下载 DuckDB 扩展
    Invoke-Native -Exe $buildPy -Arguments $bundleArgs

    if ($PythonInstaller) {
        if (-not (Test-Path $PythonInstaller)) { throw "Python 安装器不存在：$PythonInstaller" }
        Copy-Item $PythonInstaller (Join-Path $bundleDir (Split-Path -Leaf $PythonInstaller)) -Force
    } else {
        Write-Warning "未提供 -PythonInstaller，离线包将要求目标机器已装 Python"
    }

    Write-Step "打包离线 zip"
    $offZip = Join-Path $ReleaseDir "datacompare-$Version-windows-x64-offline-kit.zip"
    $zip = New-Zip $offZip
    try { Add-DirToZip -Zip $zip -Dir $bundleDir -Prefix "" } finally { $zip.Dispose() }
    Write-Host ("    " + $offZip) -ForegroundColor Green
}

Write-Step "完成，产物在 $ReleaseDir"
Get-ChildItem $ReleaseDir -File | Select-Object Name, @{N = "MB"; E = { [math]::Round($_.Length / 1MB, 2) } }
