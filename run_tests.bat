@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Tests
python -X utf8 -m unittest discover -s tests -t . -v
pause
