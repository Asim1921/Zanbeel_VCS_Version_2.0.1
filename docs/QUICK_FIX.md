# Quick Fix for Node.js Version Issue

## Problem
The frontend service is failing because Node.js 18.19.1 is too old. Vite 7 requires Node.js 20.19+ or 22.12+.

## Solution

Run this command to upgrade Node.js:

```bash
cd /home/aiuser/FoxNest
sudo ./upgrade-nodejs.sh
```

Then reinstall frontend dependencies and restart services:

```bash
cd /home/aiuser/FoxNest/foxnestFrontend
rm -rf node_modules package-lock.json
npm install

sudo systemctl restart foxnest-frontend
sudo systemctl status foxnest-frontend
```

## Alternative: Run the updated setup script

The setup script has been updated to automatically check and upgrade Node.js:

```bash
cd /home/aiuser/FoxNest
sudo ./setup-services.sh
```

This will:
1. Upgrade Node.js to version 22.x if needed
2. Reinstall frontend dependencies
3. Restart both services

