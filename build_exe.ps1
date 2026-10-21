<#
.SYNOPSIS
    Builds the FlickShare desktop app into a Windows executable with PyInstaller.
.EXAMPLE
    .\build_exe.ps1            # dist\FlickShare\FlickShare.exe (fast startup, recommended)
    .\build_exe.ps1 -OneFile   # dist\FlickShare.exe (single portable file)
#>
param(
    [switch]$OneFile,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }

if (-not $SkipInstall) {
    & $py -m pip install --quiet -r requirements.txt pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

New-Item -ItemType Directory -Force build | Out-Null
$env:QT_QPA_PLATFORM = "offscreen"
& $py -c "import sys; from PySide6.QtGui import QGuiApplication; app = QGuiApplication(sys.argv[:1]); from flickshare_app.icons import export_ico; export_ico('build/flickshare.ico')"
if ($LASTEXITCODE -ne 0) { throw "icon export failed" }
Remove-Item Env:\QT_QPA_PLATFORM

$mode = if ($OneFile) { "--onefile" } else { "--onedir" }
& $py -m PyInstaller --noconfirm --clean --windowed $mode `
    --name FlickShare `
    --icon build\flickshare.ico `
    --collect-submodules uvicorn `
    --collect-submodules zeroconf `
    --exclude-module tkinter `
    --exclude-module pytest `
    flickshare_desktop.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$exe = if ($OneFile) { "dist\FlickShare.exe" } else { "dist\FlickShare\FlickShare.exe" }
Write-Host "Built: $(Resolve-Path $exe)"
