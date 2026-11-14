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
rem After the first full setup, this just starts Blimp. To repair, run: bash setup.sh
wsl -d Ubuntu --cd "%~dp0." -e bash -c "if grep -qx BLIMP_SETUP_DONE=yes ~/blimp/config.env 2>/dev/null; then bash setup.sh --start; else bash setup.sh; fi"
echo.
rem WSL stops Ubuntu (and the agent with it) once no window is attached, so this one stays open.
wsl -d Ubuntu -e bash -c "tmux has-session -t duke 2>/dev/null || exit 0; echo; echo   Blimp is running. Keep this window open, minimised is fine: closing it stops Blimp.; while tmux has-session -t duke 2>/dev/null; do sleep 30; done; echo   Blimp stopped."
echo.
pause
