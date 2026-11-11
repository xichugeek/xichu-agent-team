@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist runtime mkdir runtime
echo stop> runtime\STOP
echo 已请求停止调度器（几秒内退出）。
