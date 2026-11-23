@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-AutoScholar.ps1" %*
if errorlevel 1 (
    echo.
    echo AutoScholar could not start. See the message above and README.md.
    pause
    exit /b 1
)
endlocal
