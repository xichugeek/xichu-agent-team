@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AI 员工团队 调度器
set PY=python
"%PY%" -X utf8 -m agent_team run
echo.
echo 调度器已退出。
pause
