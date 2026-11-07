@echo off
cd /d "%~dp0"
py -3 ensure_server.py
if errorlevel 1 (
  echo Install Python 3.10 or newer with the Python launcher, then try again.
  pause
  exit /b 1
)
if not defined BRIDGE_GUI_PORT set BRIDGE_GUI_PORT=8766
if not defined BRIDGE_HOST set BRIDGE_HOST=127.0.0.1
start "" "http://%BRIDGE_HOST%:%BRIDGE_GUI_PORT%/"
