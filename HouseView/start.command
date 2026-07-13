#!/bin/bash
# Mac launcher: double-click, or run ./start.command
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
echo ""
echo "Starting House View at http://127.0.0.1:8000/  (Ctrl+C to stop)"
python3 -m uvicorn app:app --host 127.0.0.1 --port 8000
