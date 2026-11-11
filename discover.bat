@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=python
"%PY%" -X utf8 -m agent_team discover
pause
