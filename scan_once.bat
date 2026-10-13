@echo off
rem ===========================================================================
rem  Scan once and write the snapshot into frontend/data/ (no server needed).
rem  Afterwards frontend\index.html can be opened directly by double-click.
rem
rem  Keep this file PURE ASCII - see the note in run_server.bat.
rem ===========================================================================
setlocal
cd /d "%~dp0"

chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem Prefer the py launcher (picks a Python 3 automatically), fall back to python
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)

if not defined PY (
  echo.
  echo [ERROR] Python not found. Please install Python 3.8+ and tick "Add to PATH".
  echo         https://www.python.org/downloads/windows/
  echo.
  pause
  exit /b 1
)

%PY% "backend\cli.py" --table %*

echo.
pause
endlocal
