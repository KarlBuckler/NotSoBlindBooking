#!/usr/bin/env bash
# scripts/deploy.sh — production setup for Ubuntu/Debian (headless server)
#
# Installs system dependencies, creates a virtual environment, installs
# Chromium, and registers a systemd service that starts on boot.
#
# Run as root from the repo root:
#   sudo bash scripts/deploy.sh
set -euo pipefail

SERVICE_NAME="blindbooking"
PORT=8000
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

step() { echo "==> $*"; }
ok()   { echo "    OK: $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
    fail "This script must be run as root (sudo bash scripts/deploy.sh)"
fi

# ---------------------------------------------------------------------------
# 1. System dependencies
# ---------------------------------------------------------------------------
step "Installing system dependencies ..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git
ok "System packages ready"

# ---------------------------------------------------------------------------
# 2. Check Python version (3.11+)
# ---------------------------------------------------------------------------
step "Checking Python version ..."
version=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
major=${version%%.*}
minor=${version##*.}
if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 11 ]; }; then
    fail "Python 3.11+ required (found $version). Add a PPA or compile from source."
fi
ok "Python $version"

# ---------------------------------------------------------------------------
# 3. Virtual environment
# ---------------------------------------------------------------------------
step "Creating virtual environment in $INSTALL_DIR/.venv ..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
ok "Python packages installed"

# ---------------------------------------------------------------------------
# 4. Playwright Chromium + system libs
# ---------------------------------------------------------------------------
step "Installing Chromium browser ..."
"$INSTALL_DIR/.venv/bin/playwright" install chromium
step "Installing Chromium system dependencies ..."
"$INSTALL_DIR/.venv/bin/playwright" install-deps chromium
ok "Chromium ready"

# ---------------------------------------------------------------------------
# 5. systemd service
# ---------------------------------------------------------------------------
step "Writing systemd service /etc/systemd/system/${SERVICE_NAME}.service ..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=NotSoBlindBooking — Eurowings destination finder
After=network.target

[Service]
ExecStart=${INSTALL_DIR}/.venv/bin/uvicorn app.server:app --host 0.0.0.0 --port ${PORT}
WorkingDirectory=${INSTALL_DIR}
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable  "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
ok "Service $SERVICE_NAME enabled and started"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo " Deployment complete!"
echo " Service:  $SERVICE_NAME"
echo " Port:     $PORT"
echo ""
echo " Status:   systemctl status $SERVICE_NAME"
echo " Logs:     journalctl -u $SERVICE_NAME -f"
echo " URL:      http://$(hostname -I | awk '{print $1}'):$PORT"
echo "========================================"
