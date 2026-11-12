@echo off
REM Runs the connector from source. Needs Python 3.12.
setlocal
cd /d "%~dp0"

set PY=
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY set PY=py -3.12

%PY% -c "import fastapi, uvicorn, pymobiledevice3" >nul 2>nul
if errorlevel 1 (
  echo Installing components, one moment...
  %PY% -m pip install --quiet -r requirements.txt
)

%PY% run.py
pause
