@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Codex C login
python -X utf8 -m xichu_agent login-codex codex_c
pause
