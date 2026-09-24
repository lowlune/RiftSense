@echo off
title LoL Coach UI
cd /d "%~dp0ui"
netstat -ano | findstr /c:":7777" >nul 2>nul
if errorlevel 1 start "lolcoach-ui" /min python server.py
timeout /t 2 /nobreak >nul
if exist "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" (
  start "" "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" --app=http://127.0.0.1:7777 --window-size=1500,1000 --autoplay-policy=no-user-gesture-required
) else if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
  start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --app=http://127.0.0.1:7777 --window-size=1500,1000 --autoplay-policy=no-user-gesture-required
) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
  start "" "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" --app=http://127.0.0.1:7777 --window-size=1500,1000 --autoplay-policy=no-user-gesture-required
) else (
  start "" http://127.0.0.1:7777
)
