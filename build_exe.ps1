<#
.SYNOPSIS
    Builds the FlickShare desktop app into a Windows executable with PyInstaller.
.EXAMPLE
    .\build_exe.ps1            # dist\FlickShare\FlickShare.exe (fast startup, recommended)
    .\build_exe.ps1 -OneFile   # dist\FlickShare.exe (single portable file)
#>
param(
    [switch]$OneFile,
    [switch]$SkipInstall,
    # Interpreter to use; defaults to the py launcher, else python on PATH.
    [string]$Python
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = if ($Python) { $Python } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }

# Runs the interpreter and fails on a non-zero exit code. PyInstaller logs to stderr,
# which Windows PowerShell 5 turns into terminating errors under "Stop" when redirected.
function Invoke-Py([string]$What) {
    $ErrorActionPreference = "Continue"
    & $py @args
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
}

if (-not $SkipInstall) {
    Invoke-Py "pip install" -m pip install --quiet -r requirements.txt pyinstaller
}

New-Item -ItemType Directory -Force build | Out-Null
$env:QT_QPA_PLATFORM = "offscreen"
Invoke-Py "icon export" -c "import sys; from PySide6.QtGui import QGuiApplication; app = QGuiApplication(sys.argv[:1]); from flickshare_app.icons import export_ico; export_ico('build/flickshare.ico')"
Remove-Item Env:\QT_QPA_PLATFORM

$mode = if ($OneFile) { "--onefile" } else { "--onedir" }
Invoke-Py "PyInstaller build" -m PyInstaller --noconfirm --clean --windowed $mode `
    --name FlickShare `
    --icon build\flickshare.ico `
    --collect-submodules uvicorn `
    --collect-submodules zeroconf `
    --exclude-module tkinter `
    --exclude-module pytest `
    flickshare_desktop.py

$exe = if ($OneFile) { "dist\FlickShare.exe" } else { "dist\FlickShare\FlickShare.exe" }
Write-Host "Built: $(Resolve-Path $exe)"
