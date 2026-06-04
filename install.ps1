# =============================================================================
# Evonic Platform -- Windows PowerShell Install Script
# Usage (from GitHub):  irm https://evonic.dev/install.ps1 | iex
# Usage (local source): .\install.ps1 -SourcePath "C:\path\to\evonic"
# Usage (execution policy): Set-ExecutionPolicy Bypass -Scope Process -Force; .\install.ps1
# =============================================================================

param(
    [string]$SourcePath = "",
    [string]$EvonicHome = "",
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

# ── Configuration ─────────────────────────────────────────────────────────────
if (-not $EvonicHome) {
    $EvonicHome = if ($env:EVONIC_HOME) { $env:EVONIC_HOME } else { "$env:USERPROFILE\.evonic" }
}
$RepoUrl = "https://github.com/anvie/evonic.git"
$VenvDir = "$EvonicHome\venv"
$BinDir  = "$EvonicHome\bin"
$Wrapper = "$BinDir\evonic.bat"

# ── Color helpers ─────────────────────────────────────────────────────────────
function Write-Info  { Write-Host "[INFO]    $args" -ForegroundColor Cyan }
function Write-Ok    { Write-Host "[OK]      $args" -ForegroundColor Green }
function Write-Warn  { Write-Host "[WARN]    $args" -ForegroundColor Yellow }
function Write-Err   { Write-Host "[ERROR]   $args" -ForegroundColor Red }
function Write-Step  { Write-Host ""; Write-Host "> $args" -ForegroundColor Cyan; Write-Host "" }
function Exit-Err    { Write-Err $args; exit 1 }

function Write-Banner {
    Write-Host ""
    Write-Host "___________                  .__." -ForegroundColor Cyan
    Write-Host "\_   _____/__  ______   ____ |__| ____" -ForegroundColor Cyan
    Write-Host " |    __)_\  \/ /    \ /    \|  |/ ___\" -ForegroundColor Cyan
    Write-Host " |        \\   (   O  )   |  \  \  \____" -ForegroundColor Cyan
    Write-Host "/_______  / \_/ \____/|___|  /__|\___  /" -ForegroundColor Cyan
    Write-Host "        \/                 \/        \/" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Evonic Platform Installer (Windows)" -ForegroundColor White
    Write-Host "  https://evonic.dev" -ForegroundColor Blue
    Write-Host ""
}

# ── Step 1: Prerequisite checks ───────────────────────────────────────────────
function Test-Prereqs {
    Write-Step "Step 1/6: Checking prerequisites"

    $missing = @()

    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Ok "git found"
    } else {
        Write-Err "git not found"
        $missing += "git"
    }

    $script:pythonCmd = $null
    foreach ($cmd in @("python", "python3", "py")) {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            $ver = & $cmd --version 2>&1
            if ($ver -match "Python (\d+)\.(\d+)") {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -ge 3 -and $minor -ge 8) {
                    Write-Ok "$cmd found ($ver)"
                    $script:pythonCmd = $cmd
                    break
                }
            }
        }
    }

    if (-not $script:pythonCmd) {
        Write-Err "Python 3.8+ not found"
        $missing += "python"
    }

    if ($missing.Count -gt 0) {
        Exit-Err "Missing prerequisites: $($missing -join ', '). Install them and re-run."
    }
}

# ── Step 2: Get source code ───────────────────────────────────────────────────
function Invoke-GetSource {
    Write-Step "Step 2/6: Getting Evonic source code"

    # Local source copy (dev mode)
    if ($SourcePath -and (Test-Path $SourcePath)) {
        Write-Info "Installing from local source: $SourcePath"

        if (Test-Path $EvonicHome) {
            Write-Warn "Removing existing $EvonicHome ..."
            Remove-Item -Recurse -Force $EvonicHome
        }

        Write-Info "Copying source files..."
        $excludes = @("venv", ".git", "__pycache__", "*.pyc", "node_modules")
        Copy-Item -Path $SourcePath -Destination $EvonicHome -Recurse -Force
        # Remove venv from the copy if it came along — we'll create a fresh one
        if (Test-Path "$EvonicHome\venv") {
            Remove-Item -Recurse -Force "$EvonicHome\venv"
        }
        Write-Ok "Source copied to $EvonicHome"
        return
    }

    # Git clone from GitHub
    if (Test-Path "$EvonicHome\.git") {
        Write-Info "Repository exists -- pulling latest changes..."
        $result = git -C $EvonicHome pull --ff-only origin main 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Could not pull; continuing with existing code."
        }
        Write-Ok "Repository updated"
    } elseif (Test-Path $EvonicHome) {
        Write-Warn "$EvonicHome exists but is not a git repo. Removing and re-cloning..."
        Remove-Item -Recurse -Force $EvonicHome
        git clone --depth 1 $RepoUrl $EvonicHome
        if ($LASTEXITCODE -ne 0) { Exit-Err "git clone failed. Check your internet connection." }
        Write-Ok "Repository cloned"
    } else {
        Write-Info "Cloning from $RepoUrl ..."
        git clone --depth 1 $RepoUrl $EvonicHome
        if ($LASTEXITCODE -ne 0) { Exit-Err "git clone failed. Check your internet connection or use -SourcePath for local install." }
        Write-Ok "Repository cloned to $EvonicHome"
    }

    # Verify source was obtained
    if (-not (Test-Path "$EvonicHome\cli\__main__.py")) {
        Exit-Err "Source code missing after clone. Expected $EvonicHome\cli\__main__.py"
    }
}

# ── Step 3: Create Python virtual environment ─────────────────────────────────
function New-Venv {
    Write-Step "Step 3/6: Creating Python virtual environment"

    if (Test-Path "$VenvDir\Scripts\python.exe") {
        Write-Ok "Virtual environment already exists -- skipping"
        return
    }

    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Info "Using uv to create venv (with pip seed)..."
        uv venv $VenvDir --python $script:pythonCmd --seed
        if ($LASTEXITCODE -ne 0) { Exit-Err "uv venv failed." }
    } else {
        & $script:pythonCmd -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { Exit-Err "python -m venv failed." }
    }
    Write-Ok "Virtual environment created at $VenvDir"
}

# ── Step 4: Install Python dependencies ──────────────────────────────────────
function Install-Deps {
    Write-Step "Step 4/6: Installing Python dependencies"

    $pip    = "$VenvDir\Scripts\pip.exe"
    $python = "$VenvDir\Scripts\python.exe"
    $req    = "$EvonicHome\requirements.txt"

    if (-not (Test-Path $req)) { Exit-Err "requirements.txt not found at $req" }

    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Info "Using uv pip for fast install..."
        uv pip install -r $req --python $VenvDir
        if ($LASTEXITCODE -ne 0) { Exit-Err "uv pip install failed." }
    } else {
        & $pip install --upgrade pip --quiet
        & $pip install -r $req
        if ($LASTEXITCODE -ne 0) { Exit-Err "pip install failed." }
    }
    Write-Ok "Dependencies installed"
}

# ── Step 5: Create CLI wrapper ────────────────────────────────────────────────
function New-Wrapper {
    Write-Step "Step 5/6: Creating evonic CLI wrapper"

    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

    $batLines = @(
        '@echo off',
        'setlocal',
        'chcp 65001 >nul 2>&1',
        'set "PYTHONUTF8=1"',
        'set "PYTHONIOENCODING=utf-8"',
        'for %%i in ("%~dp0..") do set "EVONIC_HOME=%%~fi"',
        'if exist "%EVONIC_HOME%\venv\Scripts\python.exe" (',
        '    "%EVONIC_HOME%\venv\Scripts\python.exe" "%EVONIC_HOME%\cli\__main__.py" %*',
        ') else (',
        '    python "%EVONIC_HOME%\cli\__main__.py" %*',
        ')',
        'endlocal'
    )
    $batLines | Set-Content -Path $Wrapper -Encoding ASCII
    Write-Ok "Wrapper created at $Wrapper"
}

# ── Step 6: Add to PATH ───────────────────────────────────────────────────────
function Add-ToPath {
    Write-Step "Step 6/6: Adding evonic to your PATH"

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $already  = ($userPath -split ";") | Where-Object { $_.TrimEnd("\") -eq $BinDir.TrimEnd("\") }

    if ($already) {
        Write-Ok "evonic is already in your PATH"
    } else {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$BinDir", "User")
        $env:Path = "$env:Path;$BinDir"
        Write-Ok "Added $BinDir to user PATH"
        Write-Info "Open a new terminal to use 'evonic' from anywhere"
    }
}

# ── Main ──────────────────────────────────────────────────────────────────────
function Main {
    Write-Banner

    if ($SourcePath) {
        Write-Host "Mode:   local source ($SourcePath)" -ForegroundColor Yellow
    } else {
        Write-Host "Mode:   clone from GitHub" -ForegroundColor Yellow
    }
    Write-Host "Target: $EvonicHome" -ForegroundColor White
    Write-Host ""
    if ($Yes) {
        Write-Info "Auto-confirming (-Yes flag set)"
    } else {
        $reply = Read-Host "Continue? [Y/n]"
        if ($reply -match "^[nN]") { Exit-Err "Installation cancelled." }
    }

    Test-Prereqs
    Invoke-GetSource
    New-Venv
    Install-Deps
    New-Wrapper
    Add-ToPath

    Write-Host ""
    Write-Host "+----------------------------------------------------------+" -ForegroundColor Green
    Write-Host "|                  Evonic installed!                       |" -ForegroundColor Green
    Write-Host "+----------------------------------------------------------+" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Next steps:" -ForegroundColor White
    Write-Host "  1. Run setup wizard:  evonic setup" -ForegroundColor Cyan
    Write-Host "  2. Start the server:  evonic start" -ForegroundColor Cyan
    Write-Host "  3. Open browser:      http://localhost:8080" -ForegroundColor Cyan
    Write-Host ""

    Write-Info "Running evonic setup..."
    & $Wrapper setup
}

Main
