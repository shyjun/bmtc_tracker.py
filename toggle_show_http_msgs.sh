#!/bin/bash

PID=$(pgrep -f "python3.*bmtc_tracker\.py" | head -1)

if [ -z "$PID" ]; then
    echo "bmtc_tracker.py is not running"
    exit 1
fi

kill -SIGUSR1 "$PID"
echo "Sent SIGUSR1 to PID $PID"
