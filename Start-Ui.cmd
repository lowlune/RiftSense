@echo off
setlocal
title LoL Coach UI
cd /d "%~dp0ui"

set "HEALTH=http://127.0.0.1:7777/api/highlight"

call :ready
if not errorlevel 1 goto open

echo Starting RiftSense UI server...
start "lolcoach-ui" /min python server.py

set /a tries=0
:wait
call :ready
if not errorlevel 1 goto open
set /a tries+=1
if %tries% geq 90 goto failed
timeout /t 2 /nobreak >nul
goto wait

:failed
echo.
echo ERROR: RiftSense UI did not answer %HEALTH% within ~180 seconds.
echo The first run may still be downloading champion/item data, or Python failed to start.
echo Close this window, run "python server.py" in the ui folder to see the error, then try again.
pause
exit /b 1

:open
if exist "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" (
  start "" "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" --app=http://127.0.0.1:7777 --window-size=1500,1000 --autoplay-policy=no-user-gesture-required
) else if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
  start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --app=http://127.0.0.1:7777 --window-size=1500,1000 --autoplay-policy=no-user-gesture-required
) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
  start "" "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" --app=http://127.0.0.1:7777 --window-size=1500,1000 --autoplay-policy=no-user-gesture-required
) else (
  start "" http://127.0.0.1:7777
)
exit /b 0

:ready
curl.exe -s --max-time 2 "%HEALTH%" 2>nul | findstr /r /c:"^{.items" >nul
if errorlevel 1 exit /b 1
exit /b 0
