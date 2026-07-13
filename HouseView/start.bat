@echo off
REM Windows / work-PC launcher. Double-click, or run start.bat
cd /d "%~dp0"
python -m pip install -r requirements.txt
echo.
echo Starting House View at http://127.0.0.1:8000/  (Ctrl+C to stop)
python -m uvicorn app:app --host 127.0.0.1 --port 8000
