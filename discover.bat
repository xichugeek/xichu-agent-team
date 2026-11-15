@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Telegram IDs
python -X utf8 -m xichu_agent discover
pause
