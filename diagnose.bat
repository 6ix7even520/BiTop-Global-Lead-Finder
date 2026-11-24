@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title BiTop Global Lead Finder V1.2.1 Diagnostic

echo ================================================
echo BiTop Global Lead Finder V1.2.1 - Diagnostic
echo ================================================
echo Folder: %CD%
echo.
echo [1] Required files:
for %%F in (app.py siwc.py index.html start.bat requirements.txt) do (
  if exist "%%F" (echo [OK] %%F) else (echo [MISSING] %%F)
)
echo.
echo [2] Python:
where py 2>nul
where python 2>nul
py -3 --version 2>nul
python --version 2>nul
echo.
echo [3] OAuth libraries:
py -3 -c "import jwt,cryptography; print('PyJWT',jwt.__version__,'cryptography',cryptography.__version__)" 2>nul
if errorlevel 1 echo [MISSING] Run start.bat to install requirements.
echo.
echo [4] Local data files (do not delete):
for %%F in (leads.sqlite3 chatgpt_auth.bin chatgpt_host_id.txt app_settings.json) do (
  if exist "%%F" (echo [FOUND] %%F) else (echo [NOT CREATED] %%F)
)
echo.
echo [5] Port 8765:
netstat -ano | findstr ":8765" || echo No process is listening on port 8765.
echo.
pause
endlocal
