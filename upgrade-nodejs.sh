#!/bin/bash
# Script to upgrade Node.js to version 22 (LTS) for FoxNest

set -e

echo "=========================================="
echo "Upgrading Node.js for FoxNest"
echo "=========================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "This script must be run with sudo"
    echo "Usage: sudo ./upgrade-nodejs.sh"
    exit 1
fi

echo "Current Node.js version: $(node --version 2>/dev/null || echo 'Not installed')"

# Install Node.js 22.x from NodeSource
echo "Installing Node.js 22.x from NodeSource..."
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs

echo ""
echo "New Node.js version: $(node --version)"
echo "New npm version: $(npm --version)"

echo ""
echo "Node.js upgrade complete!"
echo "You may need to reinstall frontend dependencies:"
echo "  cd /home/aiuser/FoxNest/foxnestFrontend"
echo "  rm -rf node_modules package-lock.json"
echo "  npm install"

