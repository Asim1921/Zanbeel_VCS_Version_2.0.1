#!/bin/bash
# Script to fix backend port conflict and restart the service

echo "Fixing backend port conflict..."

# Find and kill process on port 5000
PID=$(sudo lsof -ti :5000 2>/dev/null)
if [ ! -z "$PID" ]; then
    echo "Found process $PID using port 5000, killing it..."
    sudo kill -9 $PID
    sleep 2
fi

# Stop the service
echo "Stopping backend service..."
sudo systemctl stop foxnest-backend.service
sleep 2

# Start the service
echo "Starting backend service..."
sudo systemctl start foxnest-backend.service
sleep 3

# Check status
echo ""
echo "Backend service status:"
sudo systemctl status foxnest-backend.service --no-pager -l | head -20

echo ""
echo "Testing backend connection..."
if curl -s http://localhost:5000/ > /dev/null 2>&1; then
    echo "✓ Backend is responding!"
    curl -s http://localhost:5000/
else
    echo "✗ Backend is not responding yet. Check logs with:"
    echo "  sudo journalctl -u foxnest-backend -f"
fi

