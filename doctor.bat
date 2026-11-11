@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=python
if not exist runtime mkdir runtime
"%PY%" -X utf8 -m agent_team doctor > runtime\doctor.txt 2>&1
type runtime\doctor.txt
pause
