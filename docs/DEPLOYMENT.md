# FoxNest Deployment Guide

This guide explains how to deploy FoxNest as systemd services so it runs automatically on server startup and is accessible on the local network.

## Quick Setup

Run the setup script with sudo:

```bash
cd /home/aiuser/FoxNest
sudo ./setup-services.sh
```

This script will:
1. Install Node.js and npm if needed
2. Install frontend dependencies
3. Set up Python virtual environment and install backend dependencies
4. Install and enable systemd services
5. Start the services

## Manual Setup

If you prefer to set up manually:

### 1. Install Node.js (if not installed)

```bash
sudo apt update
sudo apt install -y nodejs npm
```

### 2. Install Frontend Dependencies

```bash
cd /home/aiuser/FoxNest/foxnestFrontend
npm install
```

### 3. Install Backend Dependencies

```bash
cd /home/aiuser/FoxNest
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Install Systemd Services

```bash
sudo cp /home/aiuser/FoxNest/foxnest-backend.service /etc/systemd/system/
sudo cp /home/aiuser/FoxNest/foxnest-frontend.service /etc/systemd/system/
sudo systemctl daemon-reload
```

### 5. Enable and Start Services

```bash
sudo systemctl enable foxnest-backend.service
sudo systemctl enable foxnest-frontend.service
sudo systemctl start foxnest-backend.service
sudo systemctl start foxnest-frontend.service
```

## Service Management

### Check Service Status

```bash
sudo systemctl status foxnest-backend
sudo systemctl status foxnest-frontend
```

### View Logs

```bash
# Backend logs
sudo journalctl -u foxnest-backend -f

# Frontend logs
sudo journalctl -u foxnest-frontend -f
```

### Restart Services

```bash
sudo systemctl restart foxnest-backend
sudo systemctl restart foxnest-frontend
```

### Stop Services

```bash
sudo systemctl stop foxnest-backend
sudo systemctl stop foxnest-frontend
```

### Disable Auto-Start (if needed)

```bash
sudo systemctl disable foxnest-backend
sudo systemctl disable foxnest-frontend
```

## Accessing FoxNest

After the services are running, you can access:

- **Backend API**: `http://<server-ip>:5000`
- **Frontend GUI**: `http://<server-ip>:5173`

To find your server's IP address:
```bash
hostname -I
```

## Service Configuration

### Backend Service
- **Port**: 5000
- **Host**: 0.0.0.0 (accessible on all network interfaces)
- **Working Directory**: `/home/aiuser/FoxNest/server`
- **Python Environment**: `/home/aiuser/FoxNest/venv`

### Frontend Service
- **Port**: 5173
- **Host**: 0.0.0.0 (accessible on all network interfaces)
- **Working Directory**: `/home/aiuser/FoxNest/foxnestFrontend`

## Troubleshooting

### Services won't start

1. Check service status:
   ```bash
   sudo systemctl status foxnest-backend
   sudo systemctl status foxnest-frontend
   ```

2. Check logs for errors:
   ```bash
   sudo journalctl -u foxnest-backend -n 50
   sudo journalctl -u foxnest-frontend -n 50
   ```

3. Verify dependencies are installed:
   - Backend: Check that `/home/aiuser/FoxNest/venv/bin/python3` exists
   - Frontend: Check that Node.js is installed and `node_modules` exists

### Port already in use

If ports 5000 or 5173 are already in use:

1. Find what's using the port:
   ```bash
   sudo lsof -i :5000
   sudo lsof -i :5173
   ```

2. Either stop the conflicting service or modify the service files to use different ports.

### Cannot access from other machines

1. Check firewall settings:
   ```bash
   sudo ufw status
   sudo ufw allow 5000/tcp
   sudo ufw allow 5173/tcp
   ```

2. Verify services are binding to 0.0.0.0:
   ```bash
   sudo netstat -tlnp | grep -E '5000|5173'
   ```

## Files Created

- `/etc/systemd/system/foxnest-backend.service` - Backend service file
- `/etc/systemd/system/foxnest-frontend.service` - Frontend service file
- `/home/aiuser/FoxNest/setup-services.sh` - Automated setup script

