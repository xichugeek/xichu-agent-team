@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -B -X utf8 demo_dashboard.py --open
if errorlevel 1 pause
