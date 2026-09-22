#!/bin/bash
# Script to fix port conflicts and restart FoxNest services

echo "Checking for processes using ports 5000 and 5173..."

# Find and kill processes on port 5000
PID_5000=$(lsof -ti :5000 2>/dev/null)
if [ ! -z "$PID_5000" ]; then
    echo "Found process $PID_5000 using port 5000, killing it..."
    kill -9 $PID_5000 2>/dev/null || sudo kill -9 $PID_5000
    sleep 2
fi

# Find and kill processes on port 5173
PID_5173=$(lsof -ti :5173 2>/dev/null)
if [ ! -z "$PID_5173" ]; then
    echo "Found process $PID_5173 using port 5173, killing it..."
    kill -9 $PID_5173 2>/dev/null || sudo kill -9 $PID_5173
    sleep 2
fi

# Stop services if running
echo "Stopping services..."
sudo systemctl stop foxnest-backend.service 2>/dev/null
sudo systemctl stop foxnest-frontend.service 2>/dev/null
sleep 2

# Start services
echo "Starting services..."
sudo systemctl start foxnest-backend.service
sleep 3
sudo systemctl start foxnest-frontend.service
sleep 3

# Check status
echo ""
echo "Service status:"
sudo systemctl status foxnest-backend.service --no-pager -l | head -15
echo ""
sudo systemctl status foxnest-frontend.service --no-pager -l | head -15

