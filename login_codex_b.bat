@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Codex B login
python -X utf8 -m xichu_agent login-codex codex_b
pause
