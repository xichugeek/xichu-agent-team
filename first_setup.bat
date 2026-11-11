@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist secrets.local.json (
  copy secrets.example.json secrets.local.json >nul
  echo 已生成 secrets.local.json，用记事本打开，把三个机器人的 token 粘进去。
) else (
  echo secrets.local.json 已存在，不覆盖。
)
notepad secrets.local.json
pause
