@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=python
if not exist runtime mkdir runtime
"%PY%" -X utf8 -m unittest discover -s tests -t . -v > runtime\tests.txt 2>&1
type runtime\tests.txt
pause
