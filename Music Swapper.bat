@echo off
cd /d "%~dp0"
where python >nul 2>nul || (echo Python not found. Install it from https://www.python.org/downloads/ and check "Add python.exe to PATH". & pause & exit /b)
python server.py
pause
