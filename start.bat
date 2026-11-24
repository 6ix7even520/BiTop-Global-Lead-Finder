@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title BiTop Global Lead Finder V1.2.1

echo ==================================================
echo   BiTop Global Lead Finder V1.2.1 - ChatGPT OAuth
echo ==================================================
echo.

if not exist "app.py" (
  echo [ERROR] app.py was not found. Please extract the full ZIP.
  pause
  exit /b 1
)
if not exist "siwc.py" (
  echo [ERROR] siwc.py was not found. Please extract the V1.2.1 package again.
  pause
  exit /b 1
)
if not exist "requirements.txt" (
  echo [ERROR] requirements.txt was not found.
  pause
  exit /b 1
)

set "PY_CMD="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 --version >nul 2>nul
  if not errorlevel 1 set "PY_CMD=py -3"
)
if not defined PY_CMD (
  where python >nul 2>nul
  if not errorlevel 1 (
    python --version >nul 2>nul
    if not errorlevel 1 set "PY_CMD=python"
  )
)
if not defined PY_CMD (
  echo [ERROR] Python 3.11 or newer was not found.
  echo Install Python from https://www.python.org/downloads/windows/
  echo Tick Add Python to PATH during installation.
  pause
  exit /b 1
)

%PY_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
  echo [ERROR] Python 3.11 or newer is required.
  pause
  exit /b 1
)

%PY_CMD% -c "import jwt, cryptography" >nul 2>nul
if errorlevel 1 (
  echo [INFO] Installing required security libraries for OAuth...
  %PY_CMD% -m pip install --upgrade pip
  %PY_CMD% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo [ERROR] Dependency installation failed.
    echo Please check your network or install requirements manually:
    echo   %PY_CMD% -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)

%PY_CMD% -c "import jwt, cryptography; print('PyJWT', jwt.__version__, '| cryptography', cryptography.__version__)"

echo.
echo Starting local server at http://127.0.0.1:8765
echo Keep this window open while using the system.
echo Press Ctrl+C to stop the system.
echo.
%PY_CMD% app.py
set "APP_EXIT=%ERRORLEVEL%"
echo.
if not "%APP_EXIT%"=="0" (
  echo [ERROR] Application stopped with exit code %APP_EXIT%.
  echo Check whether port 8765 is already in use.
) else (
  echo The application has stopped.
)
echo.
pause
endlocal
