#!/usr/bin/env bash
# scripts/setup.sh — development setup for Linux and macOS
# Run from the repo root:  bash scripts/setup.sh
set -euo pipefail

step() { echo "==> $*"; }
ok()   { echo "    OK: $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Check Python version (3.11+)
# ---------------------------------------------------------------------------
step "Checking Python version ..."

if ! command -v python3 &>/dev/null; then
    fail "python3 not found.
Ubuntu/Debian: sudo apt-get install python3 python3-venv
macOS:         brew install python"
fi

version=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
major=${version%%.*}
minor=${version##*.}

if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 11 ]; }; then
    fail "Python 3.11+ required (found $version). Install a newer Python and retry."
fi
ok "Python $version"

# ---------------------------------------------------------------------------
# 2. Ensure python3-venv is available (Ubuntu/Debian only)
# ---------------------------------------------------------------------------
if python3 -m venv --help &>/dev/null; then
    : # venv module present
else
    if command -v apt-get &>/dev/null; then
        step "Installing python3-venv ..."
        sudo apt-get install -y python3-venv
    else
        fail "python3 venv module not available. Install it for your OS and retry."
    fi
fi

# ---------------------------------------------------------------------------
# 3. Create virtual environment
# ---------------------------------------------------------------------------
step "Setting up virtual environment ..."

if [ -d ".venv" ]; then
    ok ".venv already exists — skipping creation"
else
    python3 -m venv .venv
    ok ".venv created"
fi

# ---------------------------------------------------------------------------
# 4. Install Python packages
# ---------------------------------------------------------------------------
step "Installing Python packages ..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install -r requirements.txt
ok "Packages installed"

# ---------------------------------------------------------------------------
# 5. Install Chromium via Playwright
#    On Linux, also install system-level browser dependencies.
#    On macOS, install-deps is not needed.
# ---------------------------------------------------------------------------
step "Installing Chromium browser ..."
.venv/bin/playwright install chromium

if [ "$(uname)" = "Linux" ]; then
    step "Installing Chromium system dependencies (Linux) ..."
    .venv/bin/playwright install-deps chromium
fi
ok "Chromium ready"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "======================================="
echo " Setup complete!"
echo "======================================="
echo ""
echo "Start the server:"
echo "  source .venv/bin/activate"
echo "  uvicorn app.server:app --reload"
echo ""
echo "Then open:  http://localhost:8000"
