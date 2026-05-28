@echo off
:: Launcher for setup_plugin.ps1
:: Bypasses the PowerShell execution policy for this script only.
:: Usage: setup_plugin.bat

powershell.exe -ExecutionPolicy Bypass -File "%~dp0setup_plugin.ps1"
