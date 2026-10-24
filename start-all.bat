@echo off
chcp 65001 >nul
rem 管理员启动总控；非管理员会自动弹 UAC
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1"
