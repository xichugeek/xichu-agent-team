@echo off
chcp 65001 >nul
cd /d "%~dp0"
title XichuAgent
python -X utf8 -m xichu_agent run
pause
