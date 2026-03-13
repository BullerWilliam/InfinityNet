@echo off
set FORCE_PORT_3000=1
set WAIT_TIMEOUT_MS=60000

for /f "tokens=5" %%a in ('netstat -a -n -o ^| find ":3000" ^| find "LISTENING"') do (
  taskkill /PID %%a /F >nul 2>&1
)

node server.js
