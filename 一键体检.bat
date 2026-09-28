@echo off
chcp 65001 >nul
setlocal
title Device Net Insight
cd /d "%~dp0"

echo ============================================
echo   Device Net Insight  (100%% local, no upload)
echo ============================================
echo.

set PY=
where python >nul 2>nul
if %errorlevel%==0 set PY=python
if not defined PY (
  where py >nul 2>nul
  if %errorlevel%==0 set PY=py
)
if not defined PY (
  echo [ERROR] Python not found.
  echo Please install it from https://www.python.org/downloads/
  echo and CHECK "Add Python to PATH" during setup, then run this again.
  echo.
  pause
  exit /b 1
)

%PY% -c "import matplotlib" >nul 2>nul
if errorlevel 1 (
  echo First run: installing matplotlib ...
  %PY% -m pip install --quiet matplotlib
)

echo Reading your PC's network / WiFi / Bluetooth (all local) ...
echo.
%PY% "scripts\device_net_insight.py" all -o "result"

echo.
echo Done. Results saved to: %cd%\result
if exist "%cd%\result" start "" "%cd%\result"
pause
