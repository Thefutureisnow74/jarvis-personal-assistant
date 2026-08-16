@echo off
REM Start the Jarvis wake-word listener. Close the window (or ctrl-c) to stop it.
REM listen.py picks the built-in microphone array by default; pass --device "headset"
REM (a substring is enough) to use the EarPods instead.
title JARVIS - ears
cd /d "%~dp0scripts"
echo.
echo   JARVIS is listening. Say: "Jarvis, brief me"
echo   Close this window to stop.
echo.
python listen.py %*
pause
