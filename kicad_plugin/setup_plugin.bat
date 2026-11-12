@echo off
:: Launcher for setup_plugin.ps1
:: Bypasses the PowerShell execution policy for this script only.
:: Usage: setup_plugin.bat [<path>]

powershell.exe -ExecutionPolicy Bypass -File "%~dp0setup_plugin.ps1" %*
set "rc=%ERRORLEVEL%"
if not "%rc%"=="0" (
    echo.
    echo Setup failed with exit code %rc%.
    echo Fix the error shown above and run this script again.
    pause
)
exit /b %rc%
