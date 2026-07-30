#!/bin/bash
# FoxNest Service Setup Script
# This script installs and enables the FoxNest systemd services

set -e

PROJECT_DIR="/home/aiuser/FoxNest"
SERVICE_DIR="/etc/systemd/system"

echo "=========================================="
echo "FoxNest Service Setup"
echo "=========================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "This script must be run with sudo"
    echo "Usage: sudo ./setup-services.sh"
    exit 1
fi

# Check if Node.js is installed and version is sufficient
NODE_VERSION=$(node --version 2>/dev/null || echo "")
NODE_MAJOR_VERSION=$(echo "$NODE_VERSION" | cut -d'v' -f2 | cut -d'.' -f1 || echo "0")

if [ -z "$NODE_VERSION" ]; then
    echo "Node.js is not installed. Installing Node.js 22.x..."
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y nodejs
    echo "Node.js installed successfully: $(node --version)"
elif [ "$NODE_MAJOR_VERSION" -lt 20 ]; then
    echo "Node.js version $NODE_VERSION is too old. Vite 7 requires Node.js 20.19+ or 22.12+"
    echo "Upgrading to Node.js 22.x..."
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y nodejs
    echo "Node.js upgraded to: $(node --version)"
    NODE_UPGRADED=true
else
    echo "Node.js is already installed: $NODE_VERSION"
    NODE_UPGRADED=false
fi

# Check if npm dependencies are installed (reinstall if Node.js was upgraded)
if [ ! -d "$PROJECT_DIR/foxnestFrontend/node_modules" ] || [ "$NODE_UPGRADED" = true ]; then
    echo "Installing/updating frontend dependencies..."
    cd "$PROJECT_DIR/foxnestFrontend"
    # Remove node_modules if Node.js was upgraded to avoid compatibility issues
    if [ "$NODE_UPGRADED" = true ] && [ -d "node_modules" ]; then
        echo "Node.js was upgraded, reinstalling dependencies for compatibility..."
        rm -rf node_modules package-lock.json
    fi
    npm install
    echo "Frontend dependencies installed"
else
    echo "Frontend dependencies already installed"
fi

# Check if Python virtual environment exists and has dependencies
if [ ! -f "$PROJECT_DIR/venv/bin/activate" ]; then
    echo "Creating Python virtual environment..."
    cd "$PROJECT_DIR"
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    echo "Python virtual environment created and dependencies installed"
else
    echo "Python virtual environment already exists"
fi

# Copy service files
echo "Installing systemd services..."
cp "$PROJECT_DIR/foxnest-backend.service" "$SERVICE_DIR/"
cp "$PROJECT_DIR/foxnest-frontend.service" "$SERVICE_DIR/"

# Reload systemd
echo "Reloading systemd daemon..."
systemctl daemon-reload

# Enable services to start on boot
echo "Enabling services to start on boot..."
systemctl enable foxnest-backend.service
systemctl enable foxnest-frontend.service

# Start services
echo "Starting services..."
systemctl start foxnest-backend.service
systemctl start foxnest-frontend.service

# Wait a moment for services to start
sleep 3

# Check service status
echo ""
echo "=========================================="
echo "Service Status"
echo "=========================================="
systemctl status foxnest-backend.service --no-pager -l
echo ""
systemctl status foxnest-frontend.service --no-pager -l

echo ""
echo "=========================================="
echo "Setup Complete!"
echo "=========================================="
echo "Backend API: http://$(hostname -I | awk '{print $1}'):5000"
echo "Frontend GUI: http://$(hostname -I | awk '{print $1}'):5173"
echo ""
echo "To check service status:"
echo "  sudo systemctl status foxnest-backend"
echo "  sudo systemctl status foxnest-frontend"
echo ""
echo "To view logs:"
echo "  sudo journalctl -u foxnest-backend -f"
echo "  sudo journalctl -u foxnest-frontend -f"
echo ""
echo "To restart services:"
echo "  sudo systemctl restart foxnest-backend"
echo "  sudo systemctl restart foxnest-frontend"
echo ""

