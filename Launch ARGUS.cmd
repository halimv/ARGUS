@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch-argus.ps1" %*
if errorlevel 1 pause
