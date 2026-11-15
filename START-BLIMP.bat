@echo off
title Blimp
rem Double-click to install Blimp (first time) or start it (every time after).
wsl -d Ubuntu -e true >nul 2>&1
if errorlevel 1 (
  echo Blimp runs inside Ubuntu on Windows ^(WSL^). Installing it now...
  echo Click Yes if Windows asks for permission.
  wsl --install -d Ubuntu
  echo.
  echo When that finishes: restart your PC, open Ubuntu from the Start menu once to pick a username and password,
  echo then double-click START-BLIMP.bat again.
  pause
  exit /b
)
wsl -d Ubuntu --cd "%~dp0." -e bash setup.sh
echo.
pause
