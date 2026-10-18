@echo off
cd /d %~dp0
start "yunying-server" /min cmd /c "python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 > data\uvicorn.log 2>&1"