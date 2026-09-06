@echo off
setlocal
cd /d %~dp0

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found on PATH. Install Python 3.12+ and retry.
  pause
  exit /b 1
)
where node >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Node.js not found on PATH. Install Node.js 20+ and retry.
  pause
  exit /b 1
)

if not exist sidecar\node_modules (
  echo Installing sidecar dependencies - one-time step, needs Node.js 20 or newer ...
  call npm ci --prefix sidecar --omit=dev
  if errorlevel 1 (
    echo [ERROR] Sidecar install failed. See npm output above.
    pause
    exit /b 1
  )
)

echo Starting addon + sidecar in this window (Ctrl+C stops both) ...
echo After startup, verify:  curl http://127.0.0.1:7001/health
echo Then add to Stremio:    http://127.0.0.1:7001/manifest.json
if "%PORT%"=="" set PORT=7001
if "%SIDECAR_PORT%"=="" set SIDECAR_PORT=8001
python run.py --with-sidecar
