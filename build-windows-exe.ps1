param(
  [string]$OutputDir = "dist",
  [switch]$KeepBuildDir = $true
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ResolvedOutputDir = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir $OutputDir))
$BuildRoot = "C:\temp\AristaZTPBuild"
$BuildSource = Join-Path $BuildRoot "ztp-dashboard"

function Invoke-Step {
  param([string]$FilePath, [string[]]$Arguments)
  & $FilePath @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "Command failed (exit $LASTEXITCODE): $FilePath $($Arguments -join ' ')"
  }
}

try {
  if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = "py"; $PythonArgs = @("-3")
  } elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonExe = "python"; $PythonArgs = @()
  } else {
    throw "Python 3.11 or newer is required."
  }

  New-Item -ItemType Directory -Force -Path $BuildSource | Out-Null
  robocopy $ScriptDir $BuildSource /E /XD .venv .build-venv build dist __pycache__ /XF *.pyc | Out-Null
  if ($LASTEXITCODE -gt 7) {
    throw "robocopy failed with exit code $LASTEXITCODE."
  }

  Set-Location $BuildSource
  Invoke-Step $PythonExe ($PythonArgs + @("-m", "venv", ".build-venv"))
  Invoke-Step ".\.build-venv\Scripts\python.exe" @("-m", "ensurepip", "--upgrade")
  Invoke-Step ".\.build-venv\Scripts\python.exe" @("-m", "pip", "install", "--upgrade", "--force-reinstall", "pip", "setuptools", "wheel")
  Invoke-Step ".\.build-venv\Scripts\python.exe" @("-m", "pip", "install", "-e", ".", "pyinstaller")

  Invoke-Step ".\.build-venv\Scripts\pyinstaller.exe" @(
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "AristaZTPDashboard",
    "--collect-all", "ztp_dashboard",
    "--add-data", "ztp_dashboard\data;ztp_dashboard\data",
    "ztp_dashboard\launcher.py"
  )

  New-Item -ItemType Directory -Force -Path $ResolvedOutputDir | Out-Null
  $OutputPath = Join-Path $ResolvedOutputDir "AristaZTPDashboard.exe"
  Copy-Item ".\dist\AristaZTPDashboard.exe" $OutputPath -Force
  Write-Host "Wrote $OutputPath"

  $DownloadsDir = Join-Path $env:USERPROFILE "Downloads"
  if (Test-Path $DownloadsDir) {
    $DLPath = Join-Path $DownloadsDir "AristaZTPDashboard.exe"
    Copy-Item ".\dist\AristaZTPDashboard.exe" $DLPath -Force
    Write-Host "Wrote $DLPath"
  }
} finally {
  Set-Location $ScriptDir
  if (-not $KeepBuildDir -and (Test-Path $BuildRoot)) {
    Remove-Item $BuildRoot -Recurse -Force
  } elseif ($KeepBuildDir) {
    Write-Host "Kept build directory: $BuildRoot"
  }
}
