#Requires -Version 5.1
<#
.SYNOPSIS
    One-shot development setup for Windows.
.DESCRIPTION
    Creates a virtual environment, installs Python packages, and installs
    the Chromium browser used by Playwright.
    Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
# 1. Check Python version (3.11+)
# ---------------------------------------------------------------------------
Write-Step "Checking Python version ..."

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Fail "Python not found.`nDownload Python 3.11+ from https://www.python.org/downloads/ and make sure to tick 'Add Python to PATH'."
}

$verLine = & python --version 2>&1   # "Python 3.13.1"
if ($verLine -match 'Python (\d+)\.(\d+)') {
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
} else {
    Write-Fail "Could not parse Python version from: $verLine"
}

if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
    Write-Fail "Python 3.11+ required (found $major.$minor).`nDownload from https://www.python.org/downloads/"
}
Write-Ok "Python $major.$minor — OK"

# ---------------------------------------------------------------------------
# 2. Create virtual environment
# ---------------------------------------------------------------------------
Write-Step "Setting up virtual environment ..."

if (Test-Path ".venv") {
    Write-Ok ".venv already exists — skipping creation"
} else {
    python -m venv .venv
    Write-Ok ".venv created"
}

# ---------------------------------------------------------------------------
# 3. Install Python packages
# ---------------------------------------------------------------------------
Write-Step "Installing Python packages ..."
& .\.venv\Scripts\pip install --quiet --upgrade pip
& .\.venv\Scripts\pip install -r requirements.txt
Write-Ok "Packages installed"

# ---------------------------------------------------------------------------
# 4. Install Chromium via Playwright
#    Note: playwright install-deps is Linux-only; not needed on Windows.
# ---------------------------------------------------------------------------
Write-Step "Installing Chromium browser (this may take a minute) ..."
& .\.venv\Scripts\playwright install chromium
Write-Ok "Chromium installed"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=======================================" -ForegroundColor Green
Write-Host " Setup complete!" -ForegroundColor Green
Write-Host "=======================================" -ForegroundColor Green
Write-Host ""
Write-Host "Start the server:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  uvicorn app.server:app --reload"
Write-Host ""
Write-Host "Then open:  http://localhost:8000"
Write-Host "(Chromium runs headless by default. Set `$env:HEADLESS = 'false' to show the browser window.)"
