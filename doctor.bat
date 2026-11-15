@echo off
chcp 65001 >nul
cd /d "%~dp0"
title XichuAgent doctor
python -X utf8 -m xichu_agent doctor
pause
