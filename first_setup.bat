@echo off
chcp 65001 >nul
cd /d "%~dp0"
title First setup
if not exist secrets.local.json copy secrets.example.json secrets.local.json >nul
if not exist config.local.json copy config.local.example.json config.local.json >nul
notepad config.local.json
notepad secrets.local.json
pause
