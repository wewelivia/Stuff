@echo off
REM Start the sentiment API. Run from this folder.
call conda activate julien_dev
uvicorn api_sentiment:app --host 0.0.0.0 --port 8010
pause
