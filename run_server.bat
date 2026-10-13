@echo off
rem ===========================================================================
rem  Start the AP Channel Planner local server.
rem    1) scan the Wi-Fi environment once (~2-10 s)
rem    2) serve http://127.0.0.1:8765/ and open the browser
rem  Close this window to stop the server.
rem
rem  Keep this file PURE ASCII. cmd.exe loses sync when it reads multi-byte
rem  characters from a .bat while the UTF-8 codepage is active, and then tries
rem  to run the tail of a line as a command ("is not recognized..."). Chinese
rem  output is produced by Python, which is not affected.
rem
rem  Extra arguments are passed through, e.g.  run_server.bat --port 8899
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

%PY% "backend\server.py" --preload --open %*

if errorlevel 1 (
  echo.
  echo [ERROR] The server exited.
  pause
)

endlocal
