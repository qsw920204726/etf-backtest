@echo off
cd /d %~dp0
rem ETF backtest launcher: start server if needed, wait for port, open browser
call :portcheck
if %errorlevel%==0 goto ready
echo Starting ETF backtest server, please wait...
start "ETF-Server (keep running)" /min cmd /c "py -m uvicorn api.main:app --port 8321 --host 127.0.0.1"
set /a tries=0
:waitloop
ping -n 3 127.0.0.1 >nul
call :portcheck
if %errorlevel%==0 goto ready
set /a tries+=1
if %tries% lss 45 goto waitloop
echo.
echo Server failed to start within 90 seconds.
echo Please run "py -m uvicorn api.main:app --port 8321" manually to see the error.
pause
exit /b 1
:ready
start "" http://127.0.0.1:8321
exit /b 0

:portcheck
netstat -ano | findstr ":8321" | findstr "LISTENING" >nul
exit /b %errorlevel%
